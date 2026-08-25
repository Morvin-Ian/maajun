from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from maajun.agent.core import Agent
from maajun.agent.tools import Sandbox, ToolRegistry, default_registry
from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config, RepoConfig
from maajun.daemon.core import Daemon, LocalWorkspace, make_permission_policy
from maajun.daemon.store import IncidentStore
from maajun.monitors import (
    DockerLogMonitor,
    JournaldMonitor,
    LogFileMonitor,
    Monitor,
)
from maajun.vcs import GitHubClient, GitWorkspace
from maajun.vcs.gh import remote_url

log = logging.getLogger(__name__)


class DaemonDeps:
    """Credentials and shared state common to every Daemon wiring."""

    def __init__(self, config: Config, auth: AuthManager):
        api_key = auth.get_api_key(config.ai.provider)
        if not api_key:
            raise RuntimeError(
                f"No API key for {config.ai.provider}. Run `maajun setup` "
                "to store one in the keyring."
            )

        workdir = Path(config.daemon.workdir).expanduser()
        self.store = IncidentStore(
            workdir / "incidents.db",
            reopen_after_days=config.daemon.reopen_after_days,
        )
        self.report_dir = workdir / "reports"
        # From here a failure has a database to close.
        try:
            self.wire(config, auth, workdir, api_key)
        except Exception:
            self.store.close()
            raise

    def wire(
        self, config: Config, auth: AuthManager, workdir: Path, api_key: str
    ) -> None:
        """Everything that can fail after the database is already open."""
        repos = config.github.get_all_repos()
        # GitHub is optional: with no repo, reports land on disk.
        self.local_mode = not repos
        if self.local_mode:
            self.token = None
            self.github = None
            self.repos = [RepoConfig(mode="suggest")]
            self.workspaces = {"": LocalWorkspace(local_repo_path(config))}
        else:
            token = auth.get_github_token()
            if not token:
                raise RuntimeError(
                    "A repo is configured but there is no GitHub token. "
                    "Run `maajun setup` to store one."
                )
            self.token = token
            self.github = GitHubClient(token)
            self.repos = repos
            transport = config.github.transport
            self.workspaces = {
                repo_config.repo: GitWorkspace(
                    workdir / "workspaces",
                    repo_config.repo,
                    token,
                    remote_url=remote_url(
                        repo_config.repo, transport, has_token=True
                    ),
                )
                for repo_config in repos
            }

        ai = config.ai.model_copy(update={"api_key": api_key})

        def agent_factory_for_repo(repo_config: RepoConfig, workspace) -> Callable[[], Agent]:
            def factory() -> Agent:
                return Agent(
                    Config(ai=AIProviderConfig(**ai.model_dump())),
                    # Reads are confined as tightly as writes: whatever the
                    # agent opens can be quoted in a public issue.
                    tools=default_registry(Sandbox([workspace.path])),
                    approve=make_permission_policy(repo_config.mode, workspace.path),
                    # The daily cap is only read between incidents; this is
                    # what bounds one of them.
                    cost_limit_usd=config.daemon.max_usd_per_incident,
                )
            return factory

        self.agent_factory_for_repo = agent_factory_for_repo

        def screen_factory() -> Agent:
            """A cheap agent for the one-line pre-investigation verdict.

            No tools, one round, and no model named — every provider's base
            model is its cheap tier, which is the point. `ai.triage_model`
            overrides it.
            """
            screen_ai = ai.model_copy(
                update={"model": config.ai.triage_model or None,
                        "thinking_mode": False}
            )
            return Agent(
                Config(ai=AIProviderConfig(**screen_ai.model_dump())),
                tools=ToolRegistry(),
                max_rounds=1,
            )

        self.screen_factory = screen_factory


def local_repo_path(config: Config) -> Path:
    """The checkout local mode analyzes: daemon.repo_path, else the cwd."""
    path = Path(config.daemon.repo_path or Path.cwd()).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(
            f"daemon.repo_path is not a directory: {path}. "
            "Point it at a local checkout, or configure a GitHub repo."
        )
    return path


def build_monitors(
    config: Config,
    repos: list[RepoConfig],
    *,
    backfill: bool = False,
) -> tuple[list[Monitor], dict[int, RepoConfig]]:
    """Build monitors and map each to the repo whose PRs it should open.

    Keyed on the monitor's identity, not its name: two repos can legitimately
    watch the same log file, and name-keying silently collapsed them onto
    whichever repo was configured last. The daemon owns every monitor for its
    whole lifetime, so object identity is stable.
    """
    monitor_cfg = config.monitor
    monitors: list[Monitor] = []
    monitor_to_repo: dict[int, RepoConfig] = {}

    def attach(monitor: Monitor, repo_config: RepoConfig | None) -> None:
        monitors.append(monitor)
        if repo_config is not None:
            monitor_to_repo[id(monitor)] = repo_config

    # One source can feed two repos, but watching it twice for one repo just
    # reads it twice and discards the second copy.
    watched: set[tuple[str, str, str]] = set()
    cursor_dir = Path(config.daemon.workdir).expanduser() / "cursors"

    def build_source(kind: str, target: str) -> Monitor:
        """A monitor for one (kind, target), sharing the log-parsing tuning.

        Every kind is a stream of the same log text — only where it is read
        from differs — so error_pattern, the traceback headers and the burst
        settings apply to all three.
        """
        if kind == "journald":
            return JournaldMonitor(
                target, cursor_dir=cursor_dir, backfill=backfill,
                **monitor_cfg.logfile_kwargs(),
            )
        if kind == "docker":
            return DockerLogMonitor(
                target, backfill=backfill, **monitor_cfg.logfile_kwargs()
            )
        return LogFileMonitor(
            target, cursor_dir=cursor_dir, backfill=backfill,
            **monitor_cfg.logfile_kwargs(),
        )

    def attach_source(
        kind: str, target: str, repo_config: RepoConfig | None
    ) -> None:
        key = (kind, target, repo_config.repo if repo_config else "")
        if key in watched:
            log.debug("%s %s already watched for repo %s", *key)
            return
        watched.add(key)
        attach(build_source(kind, target), repo_config)

    for repo_config, sources in config.sources_by_repo(repos):
        for kind, target in sources:
            attach_source(kind, target, repo_config)

    return monitors, monitor_to_repo


def build_daemon(
    config: Config, auth: AuthManager | None = None, *, backfill: bool = False
) -> Daemon:
    """Wire a Daemon from config + stored credentials."""
    auth = auth or AuthManager()
    deps = DaemonDeps(config, auth)
    try:
        monitors, monitor_to_repo = build_monitors(
            config, deps.repos, backfill=backfill
        )
        if not monitors:
            raise RuntimeError(
                "No monitors configured. Add log files under [monitor], or a "
                "deployment source to a repo with 'maajun discover --save'."
            )
    except Exception:
        # deps owns an open database by now.
        deps.store.close()
        raise

    return Daemon(
        config,
        monitors=monitors,
        store=deps.store,
        workspaces=deps.workspaces,
        monitor_to_repo=monitor_to_repo,
        github=deps.github,
        agent_factory_for_repo=deps.agent_factory_for_repo,
        screen_factory=deps.screen_factory,
        repo_configs=deps.repos,
        report_dir=deps.report_dir,
        local_mode=deps.local_mode,
    )


def build_daemon_for_report(config: Config, auth: AuthManager | None = None) -> Daemon:
    """Wire a Daemon for manual reports — no monitors required."""
    deps = DaemonDeps(config, auth or AuthManager())
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
