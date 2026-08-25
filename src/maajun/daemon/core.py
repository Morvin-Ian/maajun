from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

from maajun.agent.core import Correction, PermissionCallback
from maajun.agent.tools.sandbox import nearest_under
from maajun.config import Config, RepoConfig
from maajun.daemon import triage
from maajun.daemon.investigation import Investigation, Plan, bank_spend
from maajun.daemon.prompts import (
    ANALYZE_PROMPT,
    INVESTIGATION_RULES,
    MANUAL_REPORT_PROMPT,
    RECENT_COMMITS_SECTION,
    report_format,
)
from maajun.daemon.store import (
    ARTIFACT_IGNORED,
    ARTIFACT_ISSUE,
    ARTIFACT_PR,
    ARTIFACT_REPORT,
    IncidentStore,
)
from maajun.monitors import ErrorEvent, Monitor
from maajun.utils import utc_day_start_iso
from maajun.vcs import GitHubClient, GitWorkspace

log = logging.getLogger(__name__)

# Commits offered to the model for deploy blame.
RECENT_COMMIT_LIMIT = 15
LOCAL_REPO_LABEL = "(local)"

ProgressCallback = Callable[[str], None]
NoticeCallback = Callable[[str, str], None]

def no_operation(_: str) -> None:
    pass


# How much of an error reaches the prompt. The screen gets less: it only
# judges what the error says about itself, which is in the first lines.
MAX_DETAILS_IN_PROMPT = 8000
MAX_DETAILS_IN_SCREEN = 2000



# Tools fix mode may use. These are also the only gated ones, so the guard
# against anything else is a guard, not a path anything takes today — it is
# what keeps a newly gated tool from being waved through as an edit.
EDIT_TOOLS = ("edit_file", "write_file")


def make_permission_policy(mode: str, workspace: Path) -> PermissionCallback | None:
    """suggest -> None (every gated tool denied; the agent is read-only).
    fix     -> edits allowed anywhere inside the workspace clone.

    Every refusal here is a `Correction`: nothing in fix mode is forbidden,
    the call was just made against the wrong path or the wrong tool. Sent as a
    flat denial they read as "do not retry", which is how a run that was
    allowed to edit ended up publishing an analysis instead.
    """
    if mode != "fix":
        return None

    root = workspace.resolve()

    async def approve(name: str, args: dict) -> bool | str:
        if name not in EDIT_TOOLS:
            return Correction(
                f"{name} is not available here; edit files with edit_file or "
                "write_file."
            )
        path = args.get("path")
        if not path:
            return Correction(
                f"Say which file to edit: pass an absolute path under {root}."
            )
        target = Path(path).expanduser().resolve()
        if target.is_relative_to(root):
            return True
        hint = nearest_under(target, root)
        return Correction(
            f"{target} is not in the checkout. Only files under {root} can be "
            "edited — that is the branch the pull request is opened from."
            + (f" The same file is at {hint}." if hint else "")
        )

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
        return await asyncio.to_thread(self.read_commits, limit)

    def read_commits(self, limit: int) -> list[str]:
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
        screen_factory=None,
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
            screen_factory: () -> Agent for the cheap pre-investigation
                screen, or None to investigate everything the signatures let
                through. Manual reports pass None: a person who described an
                issue is not screened.
            repo_configs: Repos to act on, already normalized by DaemonDeps
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
        self.screen_factory = screen_factory
        self.repo_configs = repo_configs or config.github.get_all_repos()
        self.report_dir = report_dir or Path(config.daemon.workdir).expanduser() / "reports"
        self.local_mode = local_mode
        self.progress = progress or no_operation
        self.on_notice = on_notice
        self.budget_warned_for = ""  # UTC day already warned about
        self.handled_this_cycle = 0
        # One slot is enough: incidents are handled one at a time.
        self.last_artifact_kind: str | None = None
        self.last_ignored_reason = ""
        self.ignore_patterns = triage.compile_extra(config.monitor.ignore_patterns)
        # Per-daemon: a global one stays set, so a second Daemon in the same
        # process returned from run() without polling anything.
        self.shutdown = asyncio.Event()

    def notice(self, message: str, level: str = "info") -> None:
        if self.on_notice:
            self.on_notice(message, level)

    def over_budget(self) -> bool:
        """Whether today's spend has reached daemon.max_usd_per_day."""
        cap = self.config.daemon.max_usd_per_day
        if cap <= 0:
            return False
        day_start = utc_day_start_iso()
        spent = self.store.cost_since(day_start)
        if spent < cap:
            return False
        if self.budget_warned_for != day_start:
            self.budget_warned_for = day_start
            message = (
                # :g not :.2f — a cap of 0.005 must not be reported as $0.01.
                f"Daily spend cap reached: ${spent:.4f} of ${cap:g}. "
                "Pausing analysis until tomorrow (UTC). "
                "Raise it with 'maajun config daemon.max_usd_per_day <amount>'."
            )
            log.warning(message)
            self.notice(message, "warn")
        return True

    async def recent_commits_section(
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

    def monitors_for(self, repo_config: RepoConfig) -> list[str]:
        """Names of the monitors feeding this repo, in wiring order."""
        return [
            monitor.name for monitor in self.monitors
            if self.monitor_to_repo.get(id(monitor)) is repo_config
        ]

    def cycle_full(self) -> bool:
        """Whether this poll cycle has already analyzed its allowance.

        The daily cap bounds the day; this bounds the burst. Fifty novel
        fingerprints in one cycle would otherwise be fifty AI calls back to
        back. The remainder is picked up on the next poll.
        """
        limit = self.config.daemon.max_incidents_per_cycle
        if limit <= 0 or self.handled_this_cycle < limit:
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
        ARTIFACT_IGNORED: "Not filed — working as intended",
    }

    @staticmethod
    def artifact_label(kind: str | None) -> str:
        """A user-facing name for what an incident produced.

        Taken from what was actually published, not from repo_config.mode: a
        fix-mode incident that changed no code files an issue, and saying "PR
        opened" would send the reader looking for one that does not exist.
        """
        return Daemon.ARTIFACT_LABELS.get(kind or "", "Handled")

    def working_as_intended(self, event: ErrorEvent) -> str:
        """Why this error is a guard doing its job, or "" if it is a defect.

        The cheap pass: signatures only, no model. The expensive one is the
        agent's own verdict on the finished report, which is what catches a
        guard specific to the application.
        """
        return triage.by_design(
            event.details,
            self.ignore_patterns,
            self.config.monitor.ignore_by_design,
        )

    async def screen(self, event: ErrorEvent) -> str:
        """Why a cheap model says this is not a defect, or "" to investigate.

        The middle pass: the signatures only know an error named after its own
        intent, and the pass below — the verdict on a finished report — costs
        the whole investigation to reach. One tool-less request instead.

        Fails open. A screen that errors or cannot be parsed means the error
        is investigated; what it spent is banked either way.
        """
        if self.screen_factory is None or not self.config.daemon.screen_errors:
            return ""
        agent = self.screen_factory()
        try:
            response = await agent.chat(
                triage.SCREEN_PROMPT.format(
                    source=event.source, details=event.details[:MAX_DETAILS_IN_SCREEN],
                )
            )
        except Exception:
            log.debug(
                "the screen could not run for fp=%s; investigating instead",
                event.fingerprint, exc_info=True,
            )
            return ""
        finally:
            bank_spend(self.store, event, agent)
            await agent.aclose()
        reason = triage.screened_out(response.content)
        if reason:
            log.info(
                "screened out fp=%s before the investigation: %s",
                event.fingerprint, reason,
            )
        return reason

    def repo_for(self, monitor: Monitor) -> RepoConfig:
        """The repo a monitor's errors belong to."""
        repo_config = self.monitor_to_repo.get(id(monitor))
        if repo_config is None:
            raise ValueError(f"No repo configured for monitor: {monitor.name}")
        return repo_config

    def workspace_for(self, repo_config: RepoConfig) -> GitWorkspace:
        workspace = self.workspaces.get(repo_config.repo)
        if not workspace:
            raise ValueError(f"No workspace for repo: {repo_config.repo}")
        return workspace

    def repo_label(self, repo_config: RepoConfig) -> str:
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
        installed = self.install_signal_handlers(loop)

        try:
            while not self.shutdown.is_set():
                await self.poll_once(dry_run=dry_run)
                if once:
                    # Drain partial state, e.g. a half-read traceback.
                    for monitor in self.monitors:
                        try:
                            events = await monitor.flush()
                        except Exception:
                            log.exception("monitor %s failed to flush", monitor.name)
                            continue
                        await self.process_events(monitor, events, dry_run=dry_run)
                    return
                self.progress("Watching for errors")
                try:
                    await asyncio.wait_for(
                        self.shutdown.wait(),
                        timeout=self.config.monitor.poll_interval,
                    )
                except TimeoutError:
                    pass
            log.info("daemon shutting down gracefully")
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
            await self.aclose()

    def install_signal_handlers(self, loop) -> list[int]:
        """Ask the loop to set self.shutdown on SIGTERM/SIGINT.

        Returns what was installed so run() can remove it: the loop outlives
        one daemon. Unix-only — Windows raises NotImplementedError and
        delivers Ctrl-C as KeyboardInterrupt, which the CLI catches.
        """
        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.shutdown.set)
            except NotImplementedError:
                log.debug("this event loop has no signal handlers (%s)", sig)
                continue
            installed.append(sig)
        return installed

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

        self.handled_this_cycle = 0
        results = await asyncio.gather(*(poll(m) for m in self.monitors))

        handled: list[str] = []
        for monitor, events in zip(self.monitors, results, strict=True):
            if events is None:
                continue
            handled.extend(await self.process_events(monitor, events, dry_run=dry_run))
        return handled

    async def process_events(
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
            workspace = self.workspace_for(repo_config)
        except ValueError as exc:
            log.error("skipping monitor %s: %s", monitor.name, exc)
            self.notice(f"Monitor {monitor.name} is not usable: {exc}", "error")
            return []
        label = self.repo_label(repo_config)
        handled: list[str] = []
        for event in events:
            # The monitor knows its repo; the event does not.
            event.repo = repo_config.repo
            if not self.store.record(event):
                log.debug("known error fp=%s repo=%s", event.fingerprint, label)
                continue
            if not dry_run and (self.over_budget() or self.cycle_full()):
                # Left at 'new' for a later poll. Deleting the row instead
                # reset first_seen and the sighting count every cycle.
                log.debug(
                    "deferring fp=%s repo=%s until there is budget",
                    event.fingerprint, label,
                )
                continue
            intended = self.working_as_intended(event) or await self.screen(event)
            if intended:
                # Nothing is billed and nothing is filed, but the row stays
                # so `maajun incidents --ignored` can show the call.
                log.info(
                    "not a defect fp=%s repo=%s: %s",
                    event.fingerprint, label, intended,
                )
                self.store.mark_ignored(
                    event.fingerprint, event.repo, reason=intended
                )
                continue
            log.info(
                "new error fp=%s repo=%s: %s",
                event.fingerprint, label, event.message,
            )
            self.notice(f"New error in {label}: {event.message[:80]}", "info")
            # Counted before the attempt: a failed one still cost an AI call.
            self.handled_this_cycle += 1
            try:
                destination = await self.handle_incident(
                    event, repo_config, workspace,
                    dry_run=dry_run, progress=self.progress,
                )
                handled.append(event.fingerprint)
                if destination:
                    self.notice(
                        f"{self.artifact_label(self.last_artifact_kind)} "
                        f"for {label}: "
                        f"{destination}",
                        "success",
                    )
                elif self.last_artifact_kind == ARTIFACT_IGNORED:
                    self.notice(
                        f"Not filed for {label} — {self.last_ignored_reason}",
                        "info",
                    )
            except Exception as exc:
                log.exception("incident fp=%s repo=%s failed", event.fingerprint, label)
                self.notice(f"Incident in {label} failed: {exc}", "error")
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
        return await self.investigate(
            event, repo_config, workspace, progress,
            Plan(
                branch=f"maajun/incident-{event.fingerprint}",
                prompt=ANALYZE_PROMPT.format(
                    workspace=workspace.path,
                    source=event.source,
                    timestamp=event.timestamp,
                    details=event.details[:MAX_DETAILS_IN_PROMPT],
                    rules=INVESTIGATION_RULES,
                    format=report_format(repo_config.mode),
                ),
                # Only a fallback: the artifact is titled with what the report
                # concludes, and this raw log line is used when it concludes
                # nothing nameable.
                subject_fallback=event.message,
                commit_prefix="maajun: incident report for",
                dry_run_header=f"AI analysis for: {event.message[:80]}",
                dry_run_extra=(
                    f"Source: {event.source}  |  Fingerprint: {event.fingerprint}",
                ),
                forget_on_dry_run=True,
                blame_deploy=True,
                dry_run=dry_run,
            ),
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
        workspace = self.workspace_for(repo_config)
        event = ErrorEvent(
            source="manual",
            message=description[:200],
            details=description,
            repo=repo_config.repo,
        )
        # mark_processed only updates, so without a row a report was analyzed,
        # published, and then missing from `maajun incidents` entirely.
        self.store.record(event)
        return await self.investigate(
            event, repo_config, workspace, progress,
            Plan(
                branch=f"maajun/report-{event.fingerprint}",
                prompt=MANUAL_REPORT_PROMPT.format(
                    workspace=workspace.path,
                    description=description[:MAX_DETAILS_IN_PROMPT],
                    rules=INVESTIGATION_RULES,
                    format=report_format(repo_config.mode),
                ),
                subject_fallback=description,
                commit_prefix="maajun: manual report for",
                dry_run_header="AI analysis for manual report",
                # A dry run publishes nothing, so it should leave no incident
                # behind either — the row exists only to be marked processed.
                forget_on_dry_run=True,
                dry_run=dry_run,
            ),
        )

    async def investigate(
        self,
        event: ErrorEvent,
        repo_config: RepoConfig,
        workspace: GitWorkspace,
        progress: ProgressCallback,
        plan: Plan,
    ) -> str:
        """Run one incident and remember what it produced.

        The kind of artifact is read back off the investigation because the
        CLI reports it after the run — an issue where a PR was expected is
        the interesting case, and it is decided in there.
        """
        investigation = Investigation(
            self, event, repo_config, workspace, plan, progress
        )
        try:
            return await investigation.run()
        finally:
            if investigation.artifact_kind:
                self.last_artifact_kind = investigation.artifact_kind
            if investigation.ignored_reason:
                self.last_ignored_reason = investigation.ignored_reason
