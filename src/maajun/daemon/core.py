from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
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
from maajun.daemon.store import (
    ARTIFACT_ISSUE,
    ARTIFACT_PR,
    ARTIFACT_REPORT,
    IncidentStore,
)
from maajun.monitors import ErrorEvent, Monitor
from maajun.providers.pricing import extract_usage
from maajun.utils import utc_day_start_iso
from maajun.vcs import CommandResult, GitHubClient, GitWorkspace

log = logging.getLogger(__name__)

SHUTDOWN_EVENT = asyncio.Event()
# How many commits of history to offer the model for deploy blame.
RECENT_COMMIT_LIMIT = 15
LOCAL_REPO_LABEL = "(local)"

ProgressCallback = Callable[[str], None]
NoticeCallback = Callable[[str, str], None]

# Used since None is not callable and the daemon's constructor requires a
# callable for progress and notice.
def no_operation(_: str) -> None:
    pass


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
class LocalWorkspace:
    """Stands in for GitWorkspace when no GitHub repo is configured.

    In local mode the pipeline analyzes a checkout that is already on disk and
    writes its report beside the incident database, so there is nothing to
    clone, branch, commit, or push — but the directory is usually still a git
    checkout, and its history is worth just as much for blaming a deploy.
    """

    def __init__(self, path: Path):
        self.path = path
        self.repo = LOCAL_REPO_LABEL

    async def recent_commits(self, limit: int = 10) -> list[str]:
        """The newest commits, as "sha subject", or [] if this is not a repo.

        Local mode used to omit the "Likely cause commit" section entirely and
        always report "Unclear", purely because this method did not exist —
        the daemon probes for it with getattr and skipped the section.
        """
        return await asyncio.to_thread(self._read_commits, limit)

    def _read_commits(self, limit: int) -> list[str]:
        try:
            proc = subprocess.run(
                ["git", "log", f"-{limit}", "--no-merges", "--format=%h %s"],
                capture_output=True, text=True, timeout=10, cwd=str(self.path),
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if proc.returncode != 0:
            return []  # not a git checkout, or no commits yet
        return [line for line in proc.stdout.splitlines() if line.strip()]


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        monitors: list[Monitor],
        store: IncidentStore,
        workspaces: dict[str, GitWorkspace],
        monitor_to_repo: dict[int, RepoConfig],
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
            monitor_to_repo: Map of id(monitor) -> RepoConfig. Keyed by object
                identity so two repos can watch the same log file without one
                overwriting the other;.
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
        self.progress = progress or no_operation
        self.on_notice = on_notice
        # UTC day we have already warned about hitting the spend cap on.
        self._budget_warned_for = ""
        # Incidents analyzed so far in the current poll cycle.
        self._handled_this_cycle = 0
        # What the most recent handled incident published. Incidents are
        # processed one at a time, so a single slot is unambiguous, and it
        # spares every caller from re-deriving the artifact from the mode.
        self.last_artifact_kind: str | None = None

    def _notice(self, message: str, level: str = "info") -> None:
        if self.on_notice:
            self.on_notice(message, level)

    def _over_budget(self) -> bool:
        """Whether today's spend has reached daemon.max_usd_per_day."""
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
        # In local mode nothing pinned the checkout to base_branch, so naming
        # it would assert a branch the working copy may not be on.
        branch = (
            "the checked-out branch"
            if self.local_mode
            else (repo_config.base_branch or "the checked-out branch")
        )
        return RECENT_COMMITS_SECTION.format(
            branch=branch, commits="\n".join(commits)
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

    ARTIFACT_LABELS = {
        ARTIFACT_PR: "PR opened",
        ARTIFACT_ISSUE: "Issue opened",
        ARTIFACT_REPORT: "Report written",
    }

    @staticmethod
    def artifact_label(kind: str | None) -> str:
        """A user-facing name for what an incident produced.

        Taken from what was actually published, not from repo_config.mode: a
        fix-mode incident that changed no code files an issue, and saying "PR
        opened" would send the reader looking for one that does not exist.
        """
        return Daemon.ARTIFACT_LABELS.get(kind or "", "Handled")

    def repo_for(self, monitor: Monitor) -> RepoConfig:
        """The repo a monitor's errors belong to."""
        repo_config = self.monitor_to_repo.get(id(monitor))
        if repo_config is None:
            raise ValueError(f"No repo configured for monitor: {monitor.name}")
        return repo_config

    def _workspace_for(self, repo_config: RepoConfig) -> GitWorkspace:
        workspace = self.workspaces.get(repo_config.repo)
        if not workspace:
            raise ValueError(f"No workspace for repo: {repo_config.repo}")
        return workspace

    def _repo_label(self, repo_config: RepoConfig) -> str:
        return repo_config.repo or LOCAL_REPO_LABEL

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

        A monitor that resolves to no repo or no workspace is skipped rather
        than raised: with several repos in one daemon, one broken wiring must
        not take the other repos' monitors down with it.
        """
        try:
            repo_config = self.repo_for(monitor)
            workspace = self._workspace_for(repo_config)
        except ValueError as exc:
            log.error("skipping monitor %s: %s", monitor.name, exc)
            self._notice(f"Monitor {monitor.name} is not usable: {exc}", "error")
            return []
        label = self._repo_label(repo_config)
        handled: list[str] = []
        for event in events:
            # Attribute the error before it is recorded: the monitor knows
            # which repo it was configured for, the event does not.
            event.repo = repo_config.repo
            if not self.store.record(event):
                log.debug("known error fp=%s repo=%s", event.fingerprint, label)
                continue
            if not dry_run and (self._over_budget() or self._cycle_full()):
                # Left recorded at status 'new', which a later poll picks up.
                # This used to call forget(), deleting the row outright: while
                # the cap held, every poll re-inserted and re-deleted it, so
                # the sighting count never accumulated and first_seen ended up
                # being whenever the cap lifted rather than when the error
                # actually started.
                log.debug(
                    "deferring fp=%s repo=%s until there is budget",
                    event.fingerprint, label,
                )
                continue
            log.info(
                "new error fp=%s repo=%s: %s",
                event.fingerprint, label, event.message,
            )
            self._notice(f"New error in {label}: {event.message[:80]}", "info")
            # Counted before the attempt, not after it succeeds: the limit
            # exists to bound AI calls, and a failed incident has already
            # made (and paid for) one.
            self._handled_this_cycle += 1
            try:
                destination = await self.handle_incident(
                    event, repo_config, workspace,
                    dry_run=dry_run, progress=self.progress,
                )
                handled.append(event.fingerprint)
                if destination:
                    self._notice(
                        f"{self.artifact_label(self.last_artifact_kind)} "
                        f"for {label}: "
                        f"{destination}",
                        "success",
                    )
            except Exception as exc:
                log.exception("incident fp=%s repo=%s failed", event.fingerprint, label)
                self._notice(f"Incident in {label} failed: {exc}", "error")
                self.store.mark_failed(event.fingerprint, event.repo)
        return handled

    async def handle_incident(
        self,
        event: ErrorEvent,
        repo_config: RepoConfig,
        workspace: GitWorkspace,
        *,
        dry_run: bool = False,
        progress: ProgressCallback = no_operation,
    ) -> str:
        """Analyze one error and publish it. Returns the issue or PR URL.

        The repo and workspace are passed in by the caller, which knows the
        monitor the event came from; they are not re-derived from the event.

        When dry_run=True, skips git/GitHub operations and prints the analysis.
        """
        event.repo = repo_config.repo
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
        progress: ProgressCallback = no_operation,
    ) -> str:
        """Analyze a manually described issue. Returns the issue or PR URL."""
        workspace = self._workspace_for(repo_config)
        event = ErrorEvent(
            source="manual",
            message=description[:200],
            details=description,
            repo=repo_config.repo,
        )
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
                dry_run_header, self._repo_label(repo_config), report,
                (prompt_tokens, completion_tokens, cost), dry_run_extra,
            )
            if forget_on_dry_run:
                self.store.forget(event.fingerprint, event.repo)
            return ""

        if self.local_mode:
            return self._save_local_report(
                event, report, (prompt_tokens, completion_tokens, cost), progress
            )

        # Asked before the report file is written: that file is itself a
        # change, so checking afterwards would always say the agent edited
        # something. Fix mode is free to conclude that no code change is
        # warranted, and a "fix" PR whose entire diff is an incident report
        # wastes a review and a CI run.
        unfixed = ""
        if opens_pull_request and not await workspace.has_changes():
            log.info(
                "fix mode changed no files for fp=%s in repo=%s; filing an "
                "issue instead of an empty pull request",
                event.fingerprint, repo_config.repo,
            )
            opens_pull_request = False
            unfixed = (
                "> ℹ️ This repo is in `fix` mode, but the analysis did not "
                "change any code — so this is an issue rather than a pull "
                "request."
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
            artifact_kind = ARTIFACT_PR
        else:
            # Suggest mode changes no code, so a PR would be an empty diff that
            # still demands review and triggers CI. An issue is the artifact.
            progress("Filing issue")
            url = await self.github.create_issue(
                repo_config.repo,
                title=title,
                body=reports.issue_body(event, report, note=unfixed),
            )
            recorded_branch = ""
            artifact_kind = ARTIFACT_ISSUE

        self.last_artifact_kind = artifact_kind
        self.store.mark_processed(
            event.fingerprint,
            event.repo,
            branch=recorded_branch,
            pr_url=url,
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            report_text=report,
            artifact_kind=artifact_kind,
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
        self.last_artifact_kind = ARTIFACT_REPORT
        prompt_tokens, completion_tokens, cost = usage
        report_path = reports.write_report_file(self.report_dir, event, report)
        self.store.mark_processed(
            event.fingerprint,
            event.repo,
            branch="",
            pr_url=str(report_path),
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            report_text=report,
            artifact_kind=ARTIFACT_REPORT,
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
