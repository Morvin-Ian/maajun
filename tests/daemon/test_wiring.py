"""Tests for how monitors are wired from config + credentials."""

from maajun.auth import AuthManager
from maajun.config import Config, GitHubConfig, MonitorConfig, RepoConfig
from maajun.daemon.wiring import build_monitors


def make_config(**monitor_kwargs) -> Config:
    return Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(**monitor_kwargs),
    )


def test_actions_monitor_takes_its_token_from_the_keyring(fake_keyring):
    auth = AuthManager()
    auth.set_github_token("ghp_stored")
    config = make_config(github_actions_repos=["owner/name"])

    monitors, _ = build_monitors(config, config.github.get_all_repos(), auth)
    assert [m.name for m in monitors] == ["gh-actions:owner/name"]


def test_actions_monitor_skipped_without_a_token_but_logs(fake_keyring, caplog):
    """One unusable monitor must not stop the log monitors from running."""
    config = make_config(
        log_files=["/tmp/does-not-matter.log"], github_actions_repos=["owner/name"],
    )
    monitors, _ = build_monitors(config, config.github.get_all_repos(), AuthManager())

    assert [m.name for m in monitors] == ["logfile:/tmp/does-not-matter.log"]
    assert "no GitHub token" in caplog.text


# ---------------------------------------------------------------------------
# Monitor -> repo routing
# ---------------------------------------------------------------------------


def multi(*repos: RepoConfig, **monitor_kwargs) -> Config:
    return Config(
        github=GitHubConfig(repos=list(repos)),
        monitor=MonitorConfig(**monitor_kwargs),
    )


def routing(monitors, monitor_to_repo) -> list[tuple[str, str]]:
    """(monitor name, repo) for every monitor, in wiring order."""
    return [
        (monitor.name, monitor_to_repo[id(monitor)].repo) for monitor in monitors
    ]


def test_one_log_file_can_feed_two_repos(fake_keyring):
    """Regression: keying the map by monitor name dropped every repo but the last.

    Two services deployed from two repos can share a log file, and each needs
    its own issue.
    """
    config = multi(
        RepoConfig(repo="acme/api", log_files=["/var/log/shared.log"]),
        RepoConfig(repo="acme/web", log_files=["/var/log/shared.log"]),
    )
    monitors, monitor_to_repo = build_monitors(
        config, config.github.get_all_repos(), AuthManager()
    )

    assert routing(monitors, monitor_to_repo) == [
        ("logfile:/var/log/shared.log", "acme/api"),
        ("logfile:/var/log/shared.log", "acme/web"),
    ]


def test_a_log_file_is_not_watched_twice_for_the_same_repo(fake_keyring):
    """A path in both monitor.log_files and repo #1's log_files is one monitor."""
    config = multi(
        RepoConfig(repo="acme/api", log_files=["/var/log/api.log"]),
        RepoConfig(repo="acme/web"),
        log_files=["/var/log/api.log"],
    )
    monitors, monitor_to_repo = build_monitors(
        config, config.github.get_all_repos(), AuthManager()
    )

    assert routing(monitors, monitor_to_repo) == [
        ("logfile:/var/log/api.log", "acme/api"),
    ]


def test_global_log_files_attach_to_the_first_repo(fake_keyring):
    config = multi(
        RepoConfig(repo="acme/api"),
        RepoConfig(repo="acme/web"),
        log_files=["/var/log/app.log"],
    )
    monitors, monitor_to_repo = build_monitors(
        config, config.github.get_all_repos(), AuthManager()
    )

    assert routing(monitors, monitor_to_repo) == [
        ("logfile:/var/log/app.log", "acme/api"),
    ]


def test_actions_repo_routes_to_itself_when_configured(fake_keyring):
    auth = AuthManager()
    auth.set_github_token("ghp_stored")
    config = multi(
        RepoConfig(repo="acme/api"),
        RepoConfig(repo="acme/web"),
        github_actions_repos=["acme/web"],
    )
    monitors, monitor_to_repo = build_monitors(
        config, config.github.get_all_repos(), auth
    )

    assert routing(monitors, monitor_to_repo) == [
        ("gh-actions:acme/web", "acme/web"),
    ]


def test_unconfigured_actions_repo_warns_about_where_it_will_be_filed(
    fake_keyring, caplog
):
    """It still falls back to repo #1 — but no longer silently.

    A typo in the slug used to misfile every CI failure with no way to notice.
    """
    auth = AuthManager()
    auth.set_github_token("ghp_stored")
    config = multi(
        RepoConfig(repo="acme/api"),
        RepoConfig(repo="acme/web"),
        github_actions_repos=["acme/typo"],
    )
    monitors, monitor_to_repo = build_monitors(
        config, config.github.get_all_repos(), auth
    )

    assert routing(monitors, monitor_to_repo) == [
        ("gh-actions:acme/typo", "acme/api"),
    ]
    assert "acme/typo" in caplog.text
    assert "not a configured repo" in caplog.text
    assert "filed against acme/api" in caplog.text


# ---------------------------------------------------------------------------
# What the analysing agent may read
# ---------------------------------------------------------------------------


def test_the_daemon_agent_can_only_read_its_own_workspace(fake_keyring, tmp_path):
    """Whatever the agent opens can be quoted into a public issue or PR."""
    from pathlib import Path
    from types import SimpleNamespace

    from maajun.daemon.wiring import DaemonDeps

    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")
    auth.set_github_token("ghp_stored")
    config = make_config()
    config.daemon.workdir = str(tmp_path)

    deps = DaemonDeps(config, auth)
    workspace = SimpleNamespace(path=tmp_path / "workspaces" / "owner-name")
    agent = deps.agent_factory_for_repo(config.github.repos[0], workspace)()

    sandbox = agent.registry.sandbox
    assert sandbox is not None
    assert sandbox.contains((tmp_path / "workspaces" / "owner-name" / "src").resolve())
    assert not sandbox.contains(Path("/etc/passwd"))
    assert not sandbox.contains(tmp_path.resolve())
