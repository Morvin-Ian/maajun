"""Tests for how monitors are wired from config + credentials."""

import pytest

from maajun.auth import AuthManager
from maajun.config import Config, GitHubConfig, MonitorConfig, RepoConfig
from maajun.daemon import _build_monitors


@pytest.fixture(autouse=True)
def no_gh_cli(monkeypatch):
    monkeypatch.setattr("maajun.auth.shutil.which", lambda name: None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


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
