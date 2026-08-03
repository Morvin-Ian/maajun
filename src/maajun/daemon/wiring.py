"""Wiring — turn config plus stored credentials into a runnable Daemon."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from maajun.agent.core import Agent
from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config, RepoConfig
from maajun.daemon.core import Daemon, LocalWorkspace, make_permission_policy
from maajun.daemon.store import IncidentStore
from maajun.monitors import GitHubActionsMonitor, LogFileMonitor, Monitor
from maajun.vcs import GitHubClient, GitWorkspace

log = logging.getLogger(__name__)


class _DaemonDeps:
    """Credentials and shared state common to every Daemon wiring."""

    def __init__(self, config: Config, auth: AuthManager):
        api_key = auth.get_api_key(config.ai.provider)
        if not api_key:
            raise RuntimeError(
                f"No API key for {config.ai.provider}. Run `maajun setup` "
                f"or set {config.ai.provider.upper()}_API_KEY."
            )

        workdir = Path(config.daemon.workdir).expanduser()
        self.store = IncidentStore(workdir / "incidents.db")
        self.report_dir = workdir / "reports"

        repos = config.github.get_all_repos()
        # GitHub is optional. With no repo configured, errors are still
        # detected and analyzed — the report lands on disk instead of in a PR.
        self.local_mode = not repos
        if self.local_mode:
            self.token = None
            self.github = None
            self.repos = [RepoConfig(mode="suggest")]
            self.workspaces = {"": LocalWorkspace(_local_repo_path(config))}
        else:
            token = auth.get_github_token()
            if not token:
                raise RuntimeError(
                    "A repo is configured but there is no GitHub token. "
                    "Run `maajun setup`, export GITHUB_TOKEN, or run `gh auth login`."
                )
            self.token = token
            self.github = GitHubClient(token)
            self.repos = repos
            self.workspaces = {
                repo_config.repo: GitWorkspace(
                    workdir / "workspaces", repo_config.repo, token
                )
                for repo_config in repos
            }

        ai = config.ai.model_copy(update={"api_key": api_key})

        def agent_factory_for_repo(repo_config: RepoConfig, workspace) -> Callable[[], Agent]:
            def factory() -> Agent:
                return Agent(
                    Config(ai=AIProviderConfig(**ai.model_dump())),
                    approve=make_permission_policy(repo_config.mode, workspace.path),
                )
            return factory

        self.agent_factory_for_repo = agent_factory_for_repo


def _local_repo_path(config: Config) -> Path:
    """The checkout local mode analyzes: daemon.repo_path, else the cwd."""
    path = Path(config.daemon.repo_path or Path.cwd()).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(
            f"daemon.repo_path is not a directory: {path}. "
            "Point it at a local checkout, or configure a GitHub repo."
        )
    return path


def _build_monitors(
    config: Config, repos: list[RepoConfig], auth: AuthManager | None = None
) -> tuple[list[Monitor], dict[str, RepoConfig]]:
    """Build monitors and map each to the repo whose PRs it should open.

    `auth` supplies the GitHub Actions token, which lives in the keyring or the
    environment — never in the config file.
    """
    auth = auth or AuthManager()
    monitor_cfg = config.monitor
    monitors: list[Monitor] = []
    monitor_to_repo: dict[str, RepoConfig] = {}
    default_repo = repos[0] if repos else None

    def attach(monitor: Monitor, repo_config: RepoConfig | None) -> None:
        monitors.append(monitor)
        if repo_config is not None:
            monitor_to_repo[monitor.name] = repo_config

    # Global log_files attach to the first repo.
    for path in monitor_cfg.log_files:
        attach(LogFileMonitor(path, **monitor_cfg.logfile_kwargs()), default_repo)

    # Per-repo log_files attach to their own repo, in addition to the above.
    for repo_config in repos:
        for path in repo_config.log_files:
            attach(
                LogFileMonitor(path, **monitor_cfg.logfile_kwargs()),
                repo_config,
            )

    # GitHub Actions. Silently skipped without a token: `status` reports it,
    # and one unusable monitor should not stop the log monitors from running.
    actions_token = auth.get_github_token() if monitor_cfg.github_actions_repos else None
    if monitor_cfg.github_actions_repos and not actions_token:
        log.warning(
            "monitor.github_actions_repos is set but no GitHub token is "
            "available; skipping GitHub Actions monitors"
        )
    if actions_token:
        for repo in monitor_cfg.github_actions_repos:
            matched = next((rc for rc in repos if rc.repo == repo), default_repo)
            attach(GitHubActionsMonitor(
                actions_token,
                repo,
                burst_threshold=monitor_cfg.burst_threshold,
                burst_window_seconds=monitor_cfg.burst_window_seconds,
            ), matched)

    return monitors, monitor_to_repo


def build_daemon(config: Config, auth: AuthManager | None = None) -> Daemon:
    """Wire a Daemon from config + stored credentials.

    Supports both legacy single-repo and new multi-repo configuration.
    """
    auth = auth or AuthManager()
    deps = _DaemonDeps(config, auth)
    monitors, monitor_to_repo = _build_monitors(config, deps.repos, auth)
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
        agent_factory_for_repo=deps.agent_factory_for_repo,
        repo_configs=deps.repos,
        report_dir=deps.report_dir,
        local_mode=deps.local_mode,
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
        agent_factory_for_repo=deps.agent_factory_for_repo,
        repo_configs=deps.repos,
        report_dir=deps.report_dir,
        local_mode=deps.local_mode,
    )
