"""Tests for how monitors are wired from config + credentials."""

from maajun.auth import AuthManager
from maajun.config import Config, GitHubConfig, MonitorConfig, RepoConfig
from maajun.daemon.wiring import _build_monitors


def _config(**monitor_kwargs) -> Config:
    return Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(**monitor_kwargs),
    )


def test_actions_monitor_takes_its_token_from_the_keyring(fake_keyring):
    auth = AuthManager()
    auth.set_github_token("ghp_stored")
    config = _config(github_actions_repos=["owner/name"])

    monitors, _ = _build_monitors(config, config.github.get_all_repos(), auth)
    assert [m.name for m in monitors] == ["gh-actions:owner/name"]


def test_actions_monitor_skipped_without_a_token_but_logs(fake_keyring, caplog):
    """One unusable monitor must not stop the log monitors from running."""
    config = _config(
        log_files=["/tmp/does-not-matter.log"], github_actions_repos=["owner/name"],
    )
    monitors, _ = _build_monitors(config, config.github.get_all_repos(), AuthManager())

    assert [m.name for m in monitors] == ["logfile:/tmp/does-not-matter.log"]
    assert "no GitHub token" in caplog.text


# ---------------------------------------------------------------------------
# Monitor -> repo routing
# ---------------------------------------------------------------------------


def _multi(*repos: RepoConfig, **monitor_kwargs) -> Config:
    return Config(
        github=GitHubConfig(repos=list(repos)),
        monitor=MonitorConfig(**monitor_kwargs),
    )


def _routing(monitors, monitor_to_repo) -> list[tuple[str, str]]:
    """(monitor name, repo) for every monitor, in wiring order."""
    return [
        (monitor.name, monitor_to_repo[id(monitor)].repo) for monitor in monitors
    ]


def test_one_log_file_can_feed_two_repos(fake_keyring):
    """Regression: keying the map by monitor name dropped every repo but the last.

    Two services deployed from two repos can share a log file, and each needs
    its own issue.
    """
    config = _multi(
        RepoConfig(repo="acme/api", log_files=["/var/log/shared.log"]),
        RepoConfig(repo="acme/web", log_files=["/var/log/shared.log"]),
    )
    monitors, monitor_to_repo = _build_monitors(
        config, config.github.get_all_repos(), AuthManager()
    )

    assert _routing(monitors, monitor_to_repo) == [
        ("logfile:/var/log/shared.log", "acme/api"),
        ("logfile:/var/log/shared.log", "acme/web"),
    ]


def test_a_log_file_is_not_watched_twice_for_the_same_repo(fake_keyring):
    """A path in both monitor.log_files and repo #1's log_files is one monitor."""
    config = _multi(
        RepoConfig(repo="acme/api", log_files=["/var/log/api.log"]),
        RepoConfig(repo="acme/web"),
        log_files=["/var/log/api.log"],
    )
    monitors, monitor_to_repo = _build_monitors(
        config, config.github.get_all_repos(), AuthManager()
    )

    assert _routing(monitors, monitor_to_repo) == [
        ("logfile:/var/log/api.log", "acme/api"),
    ]


def test_global_log_files_attach_to_the_first_repo(fake_keyring):
    config = _multi(
        RepoConfig(repo="acme/api"),
        RepoConfig(repo="acme/web"),
        log_files=["/var/log/app.log"],
    )
    monitors, monitor_to_repo = _build_monitors(
        config, config.github.get_all_repos(), AuthManager()
    )

    assert _routing(monitors, monitor_to_repo) == [
        ("logfile:/var/log/app.log", "acme/api"),
    ]


def test_actions_repo_routes_to_itself_when_configured(fake_keyring):
    auth = AuthManager()
    auth.set_github_token("ghp_stored")
    config = _multi(
        RepoConfig(repo="acme/api"),
        RepoConfig(repo="acme/web"),
        github_actions_repos=["acme/web"],
    )
    monitors, monitor_to_repo = _build_monitors(
        config, config.github.get_all_repos(), auth
    )

    assert _routing(monitors, monitor_to_repo) == [
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
    config = _multi(
        RepoConfig(repo="acme/api"),
        RepoConfig(repo="acme/web"),
        github_actions_repos=["acme/typo"],
    )
    monitors, monitor_to_repo = _build_monitors(
        config, config.github.get_all_repos(), auth
    )

    assert _routing(monitors, monitor_to_repo) == [
        ("gh-actions:acme/typo", "acme/api"),
    ]
    assert "acme/typo" in caplog.text
    assert "not a configured repo" in caplog.text
    assert "filed against acme/api" in caplog.text
