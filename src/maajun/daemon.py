"""Daemon — polls monitors, analyzes new errors, opens PRs.

Flow per new error: dedup by fingerprint -> sync workspace -> branch ->
agent analyzes (and fixes, if mode allows) -> incident report committed ->
push -> pull request -> incident recorded.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from maajun.agent.core import Agent, PermissionCallback
from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config
from maajun.costs import extract_usage
from maajun.monitors import (
    ErrorEvent,
    GitHubActionsMonitor,
    LogFileMonitor,
    Monitor,
)
from maajun.notifications import Notifier
from maajun.state import IncidentStore
from maajun.vcs import GitHubClient, GitWorkspace

log = logging.getLogger(__name__)

SHUTDOWN_EVENT = asyncio.Event()

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
        workspace: GitWorkspace,
        github: GitHubClient,
        agent_factory,
        notifier: Notifier | None = None,
    ):
        """agent_factory: () -> object with `async chat(str) -> CompletionResponse`"""
        self.config = config
        self.monitors = monitors
        self.store = store
        self.workspace = workspace
        self.github = github
        self.agent_factory = agent_factory
        self.notifier = notifier or Notifier()

    async def run(self, *, once: bool = False, dry_run: bool = False) -> None:
        log.info(
            "daemon started repo=%s mode=%s monitors=%s dry_run=%s",
            self.config.github.repo,
            self.config.github.mode,
            [m.name for m in self.monitors],
            dry_run,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, SHUTDOWN_EVENT.set)

        while not SHUTDOWN_EVENT.is_set():
            await self.poll_once(dry_run=dry_run)
            if once:
                return
            try:
                await asyncio.wait_for(
                    SHUTDOWN_EVENT.wait(),
                    timeout=self.config.monitor.poll_interval,
                )
            except TimeoutError:
                pass
        log.info("daemon shutting down gracefully")

    async def poll_once(self, *, dry_run: bool = False) -> list[str]:
        """Poll all monitors; handle new incidents. Returns handled fingerprints."""
        handled: list[str] = []
        for monitor in self.monitors:
            try:
                events = await monitor.poll()
            except Exception:
                log.exception("monitor %s failed to poll", monitor.name)
                continue
            for event in events:
                if not self.store.record(event):
                    log.debug("known error fp=%s", event.fingerprint)
                    continue
                log.info("new error fp=%s: %s", event.fingerprint, event.message)
                try:
                    await self.handle_incident(event, dry_run=dry_run)
                    handled.append(event.fingerprint)
                except Exception as exc:
                    log.exception("incident fp=%s failed", event.fingerprint)
                    self.store.mark_failed(event.fingerprint)
                    await self.notifier.notify_incident_failed(
                        repo=self.config.github.repo,
                        error_message=event.message,
                        fingerprint=event.fingerprint,
                        reason=str(exc),
                    )
        return handled

    async def handle_incident(self, event: ErrorEvent, *, dry_run: bool = False) -> str:
        """Analyze one error and open a PR for it. Returns the PR URL.

        When dry_run=True, skips git/PR operations and logs what would happen.
        """
        gh = self.config.github
        branch = f"maajun/incident-{event.fingerprint}"

        if not dry_run:
            self.workspace.sync(gh.base_branch)
            self.workspace.create_branch(branch, gh.base_branch)

        prompt = ANALYZE_PROMPT.format(
            workspace=self.workspace.path,
            source=event.source,
            timestamp=event.timestamp,
            details=event.details[:8000],
        )
        if gh.mode == "fix":
            prompt += FIX_PROMPT_SUFFIX.format(workspace=self.workspace.path)

        agent = self.agent_factory()
        response = await agent.chat(prompt)
        report = response.content.strip()

        prompt_tok, comp_tok, cost = extract_usage(
            response.usage, getattr(response, "model", None)
        )

        if dry_run:
            log.info(
                "dry-run: would create branch=%s, commit report, push PR for fp=%s",
                branch,
                event.fingerprint,
            )
            log.info("dry-run report:\n%s", report[:500])
            self.store.forget(event.fingerprint)
            log.info(
                "dry-run cost: prompt=%d completion=%d cost=$%.4f",
                prompt_tok,
                comp_tok,
                cost,
            )
            return ""

        self._write_report(event, report)
        self.workspace.commit_all(f"maajun: incident report for {event.message[:60]}")
        self.workspace.push(branch)

        pr_url = await self.github.create_pull_request(
            gh.repo,
            head=branch,
            base=gh.base_branch,
            title=f"[maajun] {event.message[:80]}",
            body=self._pr_body(event, report),
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
            "opened PR %s for fp=%s (cost: $%.4f, tokens: %d/%d)",
            pr_url,
            event.fingerprint,
            cost,
            prompt_tok,
            comp_tok,
        )

        await self.notifier.notify_pr_created(
            repo=gh.repo,
            pr_url=pr_url,
            pr_title=f"[maajun] {event.message[:80]}",
            error_message=event.message,
            mode=gh.mode,
            fingerprint=event.fingerprint,
        )

        return pr_url

    def _write_report(self, event: ErrorEvent, report: str) -> None:
        report_dir = self.workspace.path / "docs" / "incidents"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{event.fingerprint}.md"
        report_path.write_text(
            f"{report}\n\n---\n\n"
            f"## Error details\n\n```\n{event.details}\n```\n\n"
            f"- Source: `{event.source}`\n"
            f"- First seen: {event.timestamp}\n"
            f"- Fingerprint: `{event.fingerprint}`\n"
        )

    def _pr_body(self, event: ErrorEvent, report: str) -> str:
        mode_note = (
            "This PR contains the applied fix and the incident report."
            if self.config.github.mode == "fix"
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


def build_daemon(config: Config, auth: AuthManager | None = None) -> Daemon:
    """Wire a Daemon from config + stored credentials."""
    auth = auth or AuthManager()

    token = auth.get_github_token()
    if not token:
        raise RuntimeError(
            "No GitHub token. Run `maajun github-login` or set GITHUB_TOKEN."
        )
    if not config.github.repo:
        raise RuntimeError("No github.repo configured. Run `maajun init` and edit the config.")

    api_key = auth.get_api_key(config.ai.provider)
    if not api_key:
        raise RuntimeError(
            f"No API key for {config.ai.provider}. Run `maajun login` "
            f"or set {config.ai.provider.upper()}_API_KEY."
        )

    monitors: list[Monitor] = [
        LogFileMonitor(path, config.monitor.error_pattern)
        for path in config.monitor.log_files
    ]
    if config.monitor.github_actions_token and config.monitor.github_actions_repos:
        for repo in config.monitor.github_actions_repos:
            monitors.append(
                GitHubActionsMonitor(config.monitor.github_actions_token, repo)
            )
    if not monitors:
        raise RuntimeError(
            "No monitors configured. Add log files under [monitor] "
            "or GitHub Actions settings."
        )

    workdir = Path(config.daemon.workdir).expanduser()
    workspace = GitWorkspace(workdir / "workspaces", config.github.repo, token)
    store = IncidentStore(workdir / "incidents.db")
    github = GitHubClient(token)

    ai = config.ai.model_copy(update={"api_key": api_key})

    def agent_factory() -> Agent:
        return Agent(
            Config(ai=AIProviderConfig(**ai.model_dump())),
            approve=make_permission_policy(config.github.mode, workspace.path),
        )

    return Daemon(
        config,
        monitors=monitors,
        store=store,
        workspace=workspace,
        github=github,
        agent_factory=agent_factory,
        notifier=Notifier(config.daemon.email),
    )
