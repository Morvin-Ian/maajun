"""The poll loop and the incident-handling flow.

Flow per new error: dedup by fingerprint -> sync workspace -> agent
analyzes -> in suggest mode a GitHub issue is filed with the report; in fix
mode the fix is committed to a branch and opened as a pull request.

Supports multiple repositories with per-repo log file mapping.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from pathlib import Path

from maajun.agent.core import PermissionCallback
from maajun.config import Config, RepoConfig
from maajun.daemon import reports
from maajun.daemon.prompts import (
    ANALYZE_PROMPT,
    FIX_PROMPT_SUFFIX,
    MANUAL_REPORT_PROMPT,
    RECENT_COMMITS_SECTION,
)
from maajun.daemon.store import IncidentStore
from maajun.monitors import ErrorEvent, Monitor
from maajun.providers.pricing import extract_usage
from maajun.utils import utc_day_start_iso
from maajun.vcs import CommandResult, GitHubClient, GitWorkspace

log = logging.getLogger(__name__)

SHUTDOWN_EVENT = asyncio.Event()

# Advances a UI spinner's phase label ("Analyzing with AI", ...).
ProgressCallback = Callable[[str], None]
# Emits a user-facing line: (message, level) where level is one of
# "info" | "success" | "warn" | "error".
NoticeCallback = Callable[[str, str], None]


def _noop(_: str) -> None:
    pass



# How many commits of history to offer the model for deploy blame.
RECENT_COMMIT_LIMIT = 15

LOCAL_REPO_LABEL = "(local)"


class LocalWorkspace:
    """Stands in for GitWorkspace when no GitHub repo is configured.

    Only `path` is used: in local mode the pipeline analyzes a checkout that
    is already on disk and writes its report beside the incident database, so
    there is nothing to clone, branch, commit, or push.
    """

    def __init__(self, path: Path):
        self.path = path
        self.repo = LOCAL_REPO_LABEL


def make_permission_policy(mode: str, workspace: Path) -> PermissionCallback | None:
    """suggest -> None (all gated tools denied, agent is read-only).
    fix     -> file edits allowed inside the workspace only; bash denied."""
    if mode != "fix":
        return None

    root = workspace.resolve()

    async def approve(name: str, args: dict) -> bool:
        if name not in ("edit_file", "write_file"):
            return False
        path = args.get("path")
        if not path:
            return False
        return Path(path).expanduser().resolve().is_relative_to(root)

    return approve


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        monitors: list[Monitor],
        store: IncidentStore,
        workspaces: dict[str, GitWorkspace],
        monitor_to_repo: dict[str, RepoConfig],
        github: GitHubClient | None,
        agent_factory_for_repo,
        repo_configs: list[RepoConfig] | None = None,
        report_dir: Path | None = None,
        local_mode: bool = False,
        progress: ProgressCallback | None = None,
        on_notice: NoticeCallback | None = None,
    ):
        """Initialize daemon with multi-repo support.

        Args:
            config: Global configuration
            monitors: List of monitors to poll
            store: Incident deduplication store
            workspaces: Map of repo name -> GitWorkspace (or LocalWorkspace)
            monitor_to_repo: Map of monitor name -> RepoConfig
            github: GitHub API client, or None in local mode
            agent_factory_for_repo: (repo_config, workspace) -> () -> Agent
            repo_configs: Repos to act on, already normalized by _DaemonDeps
            report_dir: Where local-mode reports are written
            local_mode: No GitHub configured — analyze and write reports to
                disk instead of opening pull requests
            progress: Advances a UI spinner's phase label while an incident runs
            on_notice: Emits user-facing lines (new error, PR opened, failure)
        """
        self.config = config
        self.monitors = monitors
        self.store = store
        self.workspaces = workspaces
        self.monitor_to_repo = monitor_to_repo
        self.github = github
        self.agent_factory_for_repo = agent_factory_for_repo
        self.repo_configs = repo_configs or config.github.get_all_repos()
        self.report_dir = report_dir or Path(config.daemon.workdir).expanduser() / "reports"
        self.local_mode = local_mode
        self.progress = progress or _noop
        self.on_notice = on_notice
        # UTC day we have already warned about hitting the spend cap on.
        self._budget_warned_for = ""
        # Incidents analyzed so far in the current poll cycle.
        self._handled_this_cycle = 0

    def _notice(self, message: str, level: str = "info") -> None:
        if self.on_notice:
            self.on_notice(message, level)

    def _over_budget(self) -> bool:
        """Whether today's spend has reached daemon.max_usd_per_day.

        Checked before each incident, not after: the point is to refuse the
        next AI call, and one incident's cost is only known once it is paid.
        Warns once per day rather than on every skipped event.
        """
        cap = self.config.daemon.max_usd_per_day
        if cap <= 0:
            return False
        day_start = utc_day_start_iso()
        spent = self.store.cost_since(day_start)
        if spent < cap:
            return False
        if self._budget_warned_for != day_start:
            self._budget_warned_for = day_start
            message = (
                # :g not :.2f — a cap of 0.005 must not be reported as $0.01.
                f"Daily spend cap reached: ${spent:.4f} of ${cap:g}. "
                "Pausing analysis until tomorrow (UTC). "
                "Raise it with 'maajun config daemon.max_usd_per_day <amount>'."
            )
            log.warning(message)
            self._notice(message, "warn")
        return True

    async def _recent_commits_section(
        self, repo_config: RepoConfig, workspace
    ) -> str:
        """Commit history for the prompt, so the report can blame a deploy.

        Empty when there is no history to offer (a bare local directory, a
        shallow clone) — the report simply omits the section rather than the
        model inventing a commit.
        """
        getter = getattr(workspace, "recent_commits", None)
        if getter is None:
            return ""
        try:
            commits = await getter(limit=RECENT_COMMIT_LIMIT)
        except Exception:
            log.debug("could not read recent commits", exc_info=True)
            return ""
        if not commits:
            return ""
        return RECENT_COMMITS_SECTION.format(
            branch=repo_config.base_branch or "the checked-out branch",
            commits="\n".join(commits),
        )

    def _cycle_full(self) -> bool:
        """Whether this poll cycle has already analyzed its allowance.

        The daily cap bounds the day; this bounds the burst. Fifty novel
        fingerprints in one cycle would otherwise be fifty AI calls back to
        back. The remainder is picked up on the next poll.
        """
        limit = self.config.daemon.max_incidents_per_cycle
        if limit <= 0 or self._handled_this_cycle < limit:
            return False
        log.info(
            "reached max_incidents_per_cycle (%d); remaining errors will be "
            "picked up on the next poll", limit,
        )
        return True

    def _artifact_label(self, monitor: Monitor) -> str:
        """What this monitor's incidents produce, for user-facing lines."""
        if self.local_mode:
            return "Report written"
        repo_config = self.monitor_to_repo.get(monitor.name)
        if repo_config and repo_config.mode == "fix":
            return "PR opened"
        return "Issue opened"

    def _get_repo_for_monitor(self, monitor_name: str) -> tuple[RepoConfig, GitWorkspace]:
        """Get the repo config and workspace for a given monitor."""
        repo_config = self.monitor_to_repo.get(monitor_name)
        if repo_config is None:
            raise ValueError(f"No repo configured for monitor: {monitor_name}")
        workspace = self.workspaces.get(repo_config.repo)
        if not workspace:
            raise ValueError(f"No workspace for repo: {repo_config.repo}")
        return repo_config, workspace

    async def run(self, *, once: bool = False, dry_run: bool = False) -> None:
        log.info(
            "daemon started repos=%s monitors=%s dry_run=%s local=%s",
            [rc.repo or LOCAL_REPO_LABEL for rc in self.repo_configs],
            [m.name for m in self.monitors],
            dry_run,
            self.local_mode,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, SHUTDOWN_EVENT.set)

        try:
            while not SHUTDOWN_EVENT.is_set():
                await self.poll_once(dry_run=dry_run)
                if once:
                    # Drain any carried-over partial state from monitors (e.g. a
                    # trailing traceback still in a log buffer) before exiting.
                    for monitor in self.monitors:
                        try:
                            events = await monitor.flush()
                        except Exception:
                            log.exception("monitor %s failed to flush", monitor.name)
                            continue
                        await self._process_events(monitor, events, dry_run=dry_run)
                    return
                self.progress("Watching for errors")
                try:
                    await asyncio.wait_for(
                        SHUTDOWN_EVENT.wait(),
                        timeout=self.config.monitor.poll_interval,
                    )
                except TimeoutError:
                    pass
            log.info("daemon shutting down gracefully")
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Close the GitHub client and any monitor HTTP clients."""
        if self.github is not None:
            closer = getattr(self.github, "aclose", None)
            if closer:
                await closer()
        for monitor in self.monitors:
            closer = getattr(monitor, "aclose", None)
            if closer:
                await closer()

    async def poll_once(self, *, dry_run: bool = False) -> list[str]:
        """Poll all monitors; handle new incidents. Returns handled fingerprints.

        Monitors poll concurrently (they're network-bound), but their events
        are handled sequentially so git and PR operations never race.
        """
        async def poll(monitor: Monitor) -> list[ErrorEvent] | None:
            try:
                return await monitor.poll()
            except Exception:
                log.exception("monitor %s failed to poll", monitor.name)
                return None

        self._handled_this_cycle = 0
        results = await asyncio.gather(*(poll(m) for m in self.monitors))

        handled: list[str] = []
        for monitor, events in zip(self.monitors, results, strict=True):
            if events is None:
                continue
            handled.extend(await self._process_events(monitor, events, dry_run=dry_run))
        return handled

    async def _process_events(
        self, monitor: Monitor, events: list[ErrorEvent], *, dry_run: bool
    ) -> list[str]:
        """Dedup, handle, and record a batch of events from one monitor.

        Shared by the polling loop and the --once flush so both paths dedup,
        mark failures, and send failure notifications identically.
        """
        handled: list[str] = []
        for event in events:
            if not self.store.record(event):
                log.debug("known error fp=%s", event.fingerprint)
                continue
            if not dry_run and (self._over_budget() or self._cycle_full()):
                # Forget it so a later poll treats the error as new rather
                # than silently dropping it forever.
                self.store.forget(event.fingerprint)
                continue
            log.info("new error fp=%s: %s", event.fingerprint, event.message)
            self._notice(f"New error: {event.message[:80]}", "info")
            # Counted before the attempt, not after it succeeds: the limit
            # exists to bound AI calls, and a failed incident has already
            # made (and paid for) one.
            self._handled_this_cycle += 1
            try:
                destination = await self.handle_incident(
                    event, dry_run=dry_run, progress=self.progress
                )
                handled.append(event.fingerprint)
                if destination:
                    self._notice(
                        f"{self._artifact_label(monitor)}: {destination}", "success"
                    )
            except Exception as exc:
                log.exception("incident fp=%s failed", event.fingerprint)
                self._notice(f"Incident failed: {exc}", "error")
                self.store.mark_failed(event.fingerprint)
        return handled

    async def handle_incident(
        self,
        event: ErrorEvent,
        *,
        dry_run: bool = False,
        progress: ProgressCallback = _noop,
    ) -> str:
        """Analyze one error and publish it. Returns the issue or PR URL.

        When dry_run=True, skips git/GitHub operations and prints the analysis.
        """
        repo_config, workspace = self._get_repo_for_monitor(event.source)
        prompt = ANALYZE_PROMPT.format(
            workspace=workspace.path,
            source=event.source,
            timestamp=event.timestamp,
            details=event.details[:8000],
        )
        return await self._analyze_and_open_pr(
            event,
            repo_config,
            workspace,
            branch=f"maajun/incident-{event.fingerprint}",
            prompt=prompt,
            title=f"[maajun] {event.message[:80]}",
            commit_message=f"maajun: incident report for {event.message[:60]}",
            dry_run_header=f"AI analysis for: {event.message[:80]}",
            dry_run_extra=(
                f"Source: {event.source}  |  Fingerprint: {event.fingerprint}",
            ),
            forget_on_dry_run=True,
            blame_deploy=True,
            dry_run=dry_run,
            progress=progress,
        )

    async def handle_manual_report(
        self,
        description: str,
        repo_config: RepoConfig,
        *,
        dry_run: bool = False,
        progress: ProgressCallback = _noop,
    ) -> str:
        """Analyze a manually described issue. Returns the issue or PR URL."""
        workspace = self.workspaces[repo_config.repo]
        event = ErrorEvent(source="manual", message=description[:200], details=description)
        prompt = MANUAL_REPORT_PROMPT.format(
            workspace=workspace.path,
            description=description[:8000],
        )
        return await self._analyze_and_open_pr(
            event,
            repo_config,
            workspace,
            branch=f"maajun/report-{event.fingerprint}",
            prompt=prompt,
            title=f"[maajun] {description[:80]}",
            commit_message=f"maajun: manual report for {description[:60]}",
            dry_run_header="AI analysis for manual report",
            dry_run=dry_run,
            progress=progress,
        )

    async def _analyze_and_open_pr(
        self,
        event: ErrorEvent,
        repo_config: RepoConfig,
        workspace: GitWorkspace,
        *,
        branch: str,
        prompt: str,
        title: str,
        commit_message: str,
        dry_run_header: str,
        dry_run_extra: tuple[str, ...] = (),
        forget_on_dry_run: bool = False,
        blame_deploy: bool = False,
        dry_run: bool,
        progress: ProgressCallback,
    ) -> str:
        """Shared pipeline for incident and manual reports: prepare workspace,
        run the agent, then print (dry run), write a local report, file an
        issue (suggest mode), or commit/push/open a PR (fix mode)."""
        opens_pull_request = repo_config.mode == "fix"
        if not dry_run and not self.local_mode:
            progress("Preparing workspace")
            # The clone is needed either way — the agent reads the code from
            # it — but only fix mode has a diff to put on a branch.
            await workspace.sync(repo_config.base_branch)
            if opens_pull_request:
                await workspace.create_branch(branch, repo_config.base_branch)

        # Appended after sync, not when the prompt was built: the clone has to
        # exist before there is any history to read.
        if blame_deploy:
            prompt += await self._recent_commits_section(repo_config, workspace)

        if repo_config.mode == "fix":
            prompt += FIX_PROMPT_SUFFIX.format(workspace=workspace.path)

        progress("Analyzing with AI")
        agent = self.agent_factory_for_repo(repo_config, workspace)()
        try:
            response = await agent.chat(prompt)
        finally:
            # Free the agent's HTTP client; a long watch run creates one
            # agent per incident and would otherwise leak connection pools.
            await agent.aclose()
        report = response.content.strip()
        prompt_tokens, completion_tokens, cost = extract_usage(
            response.usage, getattr(response, "model", None)
        )

        if dry_run:
            reports.print_dry_run(
                dry_run_header, repo_config.repo or LOCAL_REPO_LABEL, report,
                (prompt_tokens, completion_tokens, cost), dry_run_extra,
            )
            if forget_on_dry_run:
                self.store.forget(event.fingerprint)
            return ""

        if self.local_mode:
            return self._save_local_report(
                event, report, (prompt_tokens, completion_tokens, cost), progress
            )

        if opens_pull_request:
            verification = await self._verify(repo_config, workspace, progress)
            progress("Opening PR")
            reports.write_report_file(
                workspace.path / "docs" / "incidents", event, report
            )
            await workspace.commit_all(commit_message)
            await workspace.push(branch)
            url = await self.github.create_pull_request(
                repo_config.repo,
                head=branch,
                base=repo_config.base_branch,
                title=title,
                body=reports.pr_body(repo_config, event, report, verification),
            )
            recorded_branch = branch
        else:
            # Suggest mode changes no code, so a PR would be an empty diff that
            # still demands review and triggers CI. An issue is the artifact.
            progress("Filing issue")
            url = await self.github.create_issue(
                repo_config.repo,
                title=title,
                body=reports.issue_body(event, report),
            )
            recorded_branch = ""

        self.store.mark_processed(
            event.fingerprint,
            branch=recorded_branch,
            pr_url=url,
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        log.info(
            "opened %s %s for fp=%s in repo=%s (cost: $%.4f, tokens: %d/%d)",
            "PR" if opens_pull_request else "issue",
            url, event.fingerprint, repo_config.repo, cost,
            prompt_tokens, completion_tokens,
        )
        return url

    def _save_local_report(
        self,
        event: ErrorEvent,
        report: str,
        usage: tuple[int, int, float],
        progress: ProgressCallback,
    ) -> str:
        """Write an incident report to disk. Returns the report path.

        The local-mode counterpart to opening a PR: same analysis, same
        recorded cost, but nothing leaves the machine.
        """
        progress("Writing report")
        prompt_tokens, completion_tokens, cost = usage
        report_path = reports.write_report_file(self.report_dir, event, report)
        self.store.mark_processed(
            event.fingerprint,
            branch="",
            pr_url=str(report_path),
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        log.info(
            "wrote local report %s for fp=%s (cost: $%.4f, tokens: %d/%d)",
            report_path, event.fingerprint, cost, prompt_tokens, completion_tokens,
        )
        return str(report_path)

    async def _verify(
        self, repo_config: RepoConfig, workspace: GitWorkspace, progress: ProgressCallback
    ) -> CommandResult | None:
        """Run the repo's test_command against the agent's edits.

        Not an agent capability: the command comes from config, so the model
        cannot choose what runs. A failure is reported in the PR, not raised —
        "the fix breaks the suite" is exactly the thing a reviewer needs to
        see, and suppressing the PR would hide the analysis too.
        """
        if not repo_config.test_command:
            return None
        progress("Running tests")
        result = await workspace.run_command(repo_config.test_command)
        log.info(
            "test_command %r exited %s in repo=%s",
            repo_config.test_command, result.exit_code, repo_config.repo,
        )
        return result
