"""Daemon — polls monitors, analyzes new errors, opens PRs.

Flow per new error: dedup by fingerprint -> sync workspace -> branch ->
agent analyzes (and fixes, if mode allows) -> incident report committed ->
push -> pull request -> incident recorded.

Supports multiple repositories with per-repo log file mapping.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from pathlib import Path

from maajun.agent.core import Agent, PermissionCallback
from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config, RepoConfig
from maajun.costs import extract_usage
from maajun.monitors import ErrorEvent, Monitor
from maajun.monitors.registry import MonitorRegistry
from maajun.notifications import Notifier
from maajun.state import IncidentStore
from maajun.vcs import GitHubClient, GitWorkspace

log = logging.getLogger(__name__)

SHUTDOWN_EVENT = asyncio.Event()

# Advances a UI spinner's phase label ("Analyzing with AI", ...).
ProgressCallback = Callable[[str], None]
# Emits a user-facing line: (message, level) where level is one of
# "info" | "success" | "warn" | "error".
NoticeCallback = Callable[[str, str], None]


def _noop(_: str) -> None:
    pass

ANALYZE_PROMPT = """\
An error was detected on a monitored system. Investigate it against the
repository checked out at {workspace} and write an incident report.

Error source: {source}
First seen: {timestamp}

Error details:
```
{details}
```

Use the read_file/grep/glob/list_dir tools on {workspace} to locate the
code involved. Then respond with ONLY a markdown report in this format:

# <one-line error summary>

## What happened
<plain-language description of the error>

## Root cause
<your analysis, referencing files and lines in the repo>

## Suggested fix
<concrete change(s), with code snippets where helpful>
"""

FIX_PROMPT_SUFFIX = """
You MAY apply the fix: use edit_file/write_file on files inside {workspace}.
Keep the change minimal and focused on this error. Still finish by
responding with the markdown report, adding an "## Applied fix" section
describing exactly what you changed.
"""

MANUAL_REPORT_PROMPT = """\
A user reported the following issue. Investigate it against the
repository checked out at {workspace} and write an incident report.

Issue description:
```
{description}
```

Use the read_file/grep/glob/list_dir tools on {workspace} to locate the
code involved. Then respond with ONLY a markdown report in this format:

# <one-line error summary>

## What happened
<plain-language description of the issue>

## Root cause
<your analysis, referencing files and lines in the repo>

## Suggested fix
<concrete change(s), with code snippets where helpful>
"""


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
        github: GitHubClient,
        agent_factory_factory,
        notifier: Notifier | None = None,
        progress: ProgressCallback | None = None,
        on_notice: NoticeCallback | None = None,
    ):
        """Initialize daemon with multi-repo support.

        Args:
            config: Global configuration
            monitors: List of monitors to poll
            store: Incident deduplication store
            workspaces: Map of repo name -> GitWorkspace
            monitor_to_repo: Map of monitor name -> RepoConfig
            github: GitHub API client
            agent_factory_factory: Function that returns an agent_factory for a repo config
            notifier: Email notifier
            progress: Advances a UI spinner's phase label while an incident runs
            on_notice: Emits user-facing lines (new error, PR opened, failure)
        """
        self.config = config
        self.monitors = monitors
        self.store = store
        self.workspaces = workspaces
        self.monitor_to_repo = monitor_to_repo
        self.github = github
        self.agent_factory_factory = agent_factory_factory
        self.notifier = notifier or Notifier()
        self.progress = progress or _noop
        self.on_notice = on_notice

    def _notice(self, message: str, level: str = "info") -> None:
        if self.on_notice:
            self.on_notice(message, level)

    def _get_repo_for_monitor(self, monitor_name: str) -> tuple[RepoConfig, GitWorkspace]:
        """Get the repo config and workspace for a given monitor."""
        repo_config = self.monitor_to_repo.get(monitor_name)
        if not repo_config:
            raise ValueError(f"No repo configured for monitor: {monitor_name}")
        workspace = self.workspaces.get(repo_config.repo)
        if not workspace:
            raise ValueError(f"No workspace for repo: {repo_config.repo}")
        return repo_config, workspace

    async def run(self, *, once: bool = False, dry_run: bool = False) -> None:
        repos = [rc.repo for rc in self.config.github.get_all_repos()]
        log.info(
            "daemon started repos=%s monitors=%s dry_run=%s",
            repos,
            [m.name for m in self.monitors],
            dry_run,
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
            log.info("new error fp=%s: %s", event.fingerprint, event.message)
            self._notice(f"New error: {event.message[:80]}", "info")
            try:
                pr_url = await self.handle_incident(
                    event, dry_run=dry_run, progress=self.progress
                )
                handled.append(event.fingerprint)
                if pr_url:
                    self._notice(f"PR opened: {pr_url}", "success")
            except Exception as exc:
                log.exception("incident fp=%s failed", event.fingerprint)
                self._notice(f"Incident failed: {exc}", "error")
                self.store.mark_failed(event.fingerprint)
                repo_config = self.monitor_to_repo.get(monitor.name)
                await self.notifier.notify_incident_failed(
                    repo=repo_config.repo if repo_config else "unknown",
                    error_message=event.message,
                    fingerprint=event.fingerprint,
                    reason=str(exc),
                )
        return handled

    async def handle_incident(
        self,
        event: ErrorEvent,
        *,
        dry_run: bool = False,
        progress: ProgressCallback = _noop,
    ) -> str:
        """Analyze one error and open a PR for it. Returns the PR URL.

        When dry_run=True, skips git/PR operations and prints the analysis.
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
        """Analyze a manually described issue and open a PR. Returns the PR URL."""
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
        dry_run: bool,
        progress: ProgressCallback,
    ) -> str:
        """Shared pipeline for incident and manual reports: prepare workspace,
        run the agent, then either print (dry run) or commit/push/open a PR."""
        if not dry_run:
            progress("Preparing workspace")
            await workspace.sync(repo_config.base_branch)
            await workspace.create_branch(branch, repo_config.base_branch)

        if repo_config.mode == "fix":
            prompt += FIX_PROMPT_SUFFIX.format(workspace=workspace.path)

        progress("Analyzing with AI")
        agent = self.agent_factory_factory(repo_config, workspace)()
        try:
            response = await agent.chat(prompt)
        finally:
            # Free the agent's HTTP client; a long watch run creates one
            # agent per incident and would otherwise leak connection pools.
            await agent.aclose()
        report = response.content.strip()
        prompt_tok, comp_tok, cost = extract_usage(
            response.usage, getattr(response, "model", None)
        )

        if dry_run:
            self._print_dry_run(
                dry_run_header, repo_config.repo, report,
                (prompt_tok, comp_tok, cost), dry_run_extra,
            )
            if forget_on_dry_run:
                self.store.forget(event.fingerprint)
            return ""

        progress("Opening PR")
        self._write_report(workspace, event, report)
        await workspace.commit_all(commit_message)
        await workspace.push(branch)

        pr_url = await self.github.create_pull_request(
            repo_config.repo,
            head=branch,
            base=repo_config.base_branch,
            title=title,
            body=self._pr_body(repo_config, event, report),
        )
        self.store.mark_processed(
            event.fingerprint,
            branch=branch,
            pr_url=pr_url,
            cost_usd=cost,
            prompt_tokens=prompt_tok,
            completion_tokens=comp_tok,
        )
        log.info(
            "opened PR %s for fp=%s in repo=%s (cost: $%.4f, tokens: %d/%d)",
            pr_url, event.fingerprint, repo_config.repo, cost, prompt_tok, comp_tok,
        )
        await self.notifier.notify_pr_created(
            repo=repo_config.repo,
            pr_url=pr_url,
            pr_title=title,
            error_message=event.message,
            mode=repo_config.mode,
            fingerprint=event.fingerprint,
        )
        return pr_url

    @staticmethod
    def _print_dry_run(
        header: str,
        repo: str,
        report: str,
        usage: tuple[int, int, float],
        extra: tuple[str, ...] = (),
    ) -> None:
        prompt_tok, comp_tok, cost = usage
        bar = "=" * 60
        print(f"\n{bar}")
        print(f"DRY RUN — {header}")
        for line in extra:
            print(line)
        print(f"Repo: {repo}")
        print(f"{bar}\n")
        print(report)
        print(f"\n{bar}")
        print(f"Cost: {prompt_tok} prompt + {comp_tok} completion tokens = ${cost:.4f}")
        print(f"{bar}\n")

    def _write_report(self, workspace: GitWorkspace, event: ErrorEvent, report: str) -> None:
        report_dir = workspace.path / "docs" / "incidents"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{event.fingerprint}.md"
        report_path.write_text(
            f"{report}\n\n---\n\n"
            f"## Error details\n\n```\n{event.details}\n```\n\n"
            f"- Source: `{event.source}`\n"
            f"- First seen: {event.timestamp}\n"
            f"- Fingerprint: `{event.fingerprint}`\n"
        )

    def _pr_body(self, repo_config: RepoConfig, event: ErrorEvent, report: str) -> str:
        mode_note = (
            "This PR contains the applied fix and the incident report."
            if repo_config.mode == "fix"
            else "This PR contains the incident report only (suggest mode) — "
            "no code was changed."
        )
        return (
            f"{report}\n\n---\n"
            f"{mode_note}\n\n"
            f"- Source: `{event.source}`\n"
            f"- Fingerprint: `{event.fingerprint}`\n"
            f"- Opened automatically by [maajun](https://github.com/Morvin-Ian/maajun)."
        )


class _DaemonDeps:
    """Credentials and shared state common to every Daemon wiring."""

    def __init__(self, config: Config, auth: AuthManager):
        token = auth.get_github_token()
        if not token:
            raise RuntimeError(
                "No GitHub token. Export GITHUB_TOKEN or run `gh auth login`."
            )

        repos = config.github.get_all_repos()
        if not repos:
            raise RuntimeError(
                "No github.repo configured. Run `maajun init` and edit the config."
            )

        api_key = auth.get_api_key(config.ai.provider)
        if not api_key:
            raise RuntimeError(
                f"No API key for {config.ai.provider}. Run `maajun login` "
                f"or set {config.ai.provider.upper()}_API_KEY."
            )

        workdir = Path(config.daemon.workdir).expanduser()
        self.token = token
        self.repos = repos
        self.workspaces: dict[str, GitWorkspace] = {}
        for repo_config in repos:
            if repo_config.repo not in self.workspaces:
                self.workspaces[repo_config.repo] = GitWorkspace(
                    workdir / "workspaces", repo_config.repo, token
                )
        self.store = IncidentStore(workdir / "incidents.db")
        self.github = GitHubClient(token)

        ai = config.ai.model_copy(update={"api_key": api_key})

        def agent_factory_factory(repo_config: RepoConfig, workspace: GitWorkspace):
            def factory() -> Agent:
                return Agent(
                    Config(ai=AIProviderConfig(**ai.model_dump())),
                    approve=make_permission_policy(repo_config.mode, workspace.path),
                )
            return factory

        self.agent_factory_factory = agent_factory_factory


def _build_monitors(
    config: Config, repos: list[RepoConfig]
) -> tuple[list[Monitor], dict[str, RepoConfig]]:
    """Build monitors and map each to the repo whose PRs it should open."""
    monitor_cfg = config.monitor
    monitors: list[Monitor] = []
    monitor_to_repo: dict[str, RepoConfig] = {}
    default_repo = repos[0] if repos else None

    def attach(monitor: Monitor, repo_config: RepoConfig | None) -> None:
        monitors.append(monitor)
        if repo_config is not None:
            monitor_to_repo[monitor.name] = repo_config

    def create(monitor_type: str, **kwargs) -> Monitor:
        """Build a monitor, turning config mistakes into readable errors.

        A bad `type` or a stray key in [[monitor.instances]] otherwise
        surfaces as a bare ValueError/TypeError traceback at startup.
        """
        try:
            return MonitorRegistry.create(monitor_type, **kwargs)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        except TypeError as e:
            raise RuntimeError(
                f"Invalid settings for monitor type {monitor_type!r}: {e}"
            ) from e

    # Shorthand: global log_files attach to the first repo.
    for path in monitor_cfg.log_files:
        attach(create("logfile", path=path, **monitor_cfg.logfile_kwargs()), default_repo)

    # Shorthand: per-repo log_files attach to their own repo, in addition
    # to the above.
    for repo_config in repos:
        for path in repo_config.log_files:
            attach(
                create("logfile", path=path, **monitor_cfg.logfile_kwargs()),
                repo_config,
            )

    # Shorthand: GitHub Actions.
    if monitor_cfg.github_actions_token and monitor_cfg.github_actions_repos:
        for repo in monitor_cfg.github_actions_repos:
            matched = next((rc for rc in repos if rc.repo == repo), default_repo)
            attach(create(
                "github-actions",
                token=monitor_cfg.github_actions_token,
                repo=repo,
                burst_threshold=monitor_cfg.burst_threshold,
                burst_window_seconds=monitor_cfg.burst_window_seconds,
            ), matched)

    # Declarative instances — any registered monitor type.
    for instance in monitor_cfg.instances:
        repo_config = default_repo
        if instance.repo:
            repo_config = next((rc for rc in repos if rc.repo == instance.repo), None)
            if repo_config is None:
                log.warning(
                    "monitor instance %r targets repo %r, which is not configured; "
                    "its events will be analyzed but will not open PRs",
                    instance.type, instance.repo,
                )
        attach(create(instance.type, **instance.monitor_kwargs()), repo_config)

    return monitors, monitor_to_repo


def build_daemon(config: Config, auth: AuthManager | None = None) -> Daemon:
    """Wire a Daemon from config + stored credentials.

    Supports both legacy single-repo and new multi-repo configuration.
    """
    deps = _DaemonDeps(config, auth or AuthManager())
    monitors, monitor_to_repo = _build_monitors(config, deps.repos)
    if not monitors:
        raise RuntimeError(
            "No monitors configured. Add log files under [monitor] "
            "or GitHub Actions settings."
        )

    return Daemon(
        config,
        monitors=monitors,
        store=deps.store,
        workspaces=deps.workspaces,
        monitor_to_repo=monitor_to_repo,
        github=deps.github,
        agent_factory_factory=deps.agent_factory_factory,
        notifier=Notifier(config.daemon.email),
    )


def build_daemon_for_report(config: Config, auth: AuthManager | None = None) -> Daemon:
    """Wire a Daemon for manual reports — no monitors required."""
    deps = _DaemonDeps(config, auth or AuthManager())
    return Daemon(
        config,
        monitors=[],
        store=deps.store,
        workspaces=deps.workspaces,
        monitor_to_repo={},
        github=deps.github,
        agent_factory_factory=deps.agent_factory_factory,
        notifier=Notifier(config.daemon.email),
    )
