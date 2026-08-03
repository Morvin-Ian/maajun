"""Tests for `maajun setup` — the one-command wizard."""

import subprocess

import pytest
from typer.testing import CliRunner

from maajun.auth import AuthManager
from maajun.cli import app
from maajun.cli.wizard import detect_repo_from_git
from maajun.config import Config

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_gh_cli(monkeypatch):
    """Keep the host's real gh login out of the tests."""
    monkeypatch.setattr("maajun.auth.shutil.which", lambda name: None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")


@pytest.fixture
def no_git_detect(monkeypatch):
    """Stop the wizard picking up the repo maajun itself is developed in."""
    monkeypatch.setattr("maajun.cli.wizard.detect_repo_from_git", lambda *a: None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Never let the push-access probe reach the real GitHub API."""
    class _Client:
        def __init__(self, token, **kwargs):
            self.token = token

        async def validate_token(self):
            return "tester"

        async def can_push(self, repo):
            return True

        async def aclose(self):
            pass

    monkeypatch.setattr("maajun.cli.wizard.GitHubClient", _Client)


# ---------------------------------------------------------------------------
# Repo autodetection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:owner/name.git", "owner/name"),
    ("https://github.com/owner/name", "owner/name"),
    ("https://github.com/owner/name.git", "owner/name"),
    ("https://github.com/owner/name/", "owner/name"),
    ("https://gitlab.com/owner/name.git", None),
    ("", None),
])
def test_detect_repo_parses_remote_forms(monkeypatch, url, expected):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=url, stderr="")

    monkeypatch.setattr("maajun.cli.wizard.subprocess.run", run)
    assert detect_repo_from_git() == expected


def test_detect_repo_is_quiet_outside_a_checkout(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr("maajun.cli.wizard.subprocess.run", run)
    assert detect_repo_from_git() is None


# ---------------------------------------------------------------------------
# Non-interactive setup
# ---------------------------------------------------------------------------


def test_setup_needs_only_an_api_key(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(
        app, ["setup", "--non-interactive", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    assert config_path.exists()


def test_setup_fails_without_an_api_key(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config_path = tmp_path / "config.toml"
    result = runner.invoke(
        app, ["setup", "--non-interactive", "--config", str(config_path)]
    )
    assert result.exit_code == 1
    assert "No API key" in result.output


def test_setup_never_writes_the_placeholder_repo(fake_keyring, api_key, tmp_path):
    """Regression: a written placeholder read back as a configured repo, so the
    daemon demanded a token to push to owner/name."""
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["setup", "--non-interactive", "--config", str(config_path)])

    assert 'repo = "owner/name"' not in config_path.read_text()
    assert Config.load(config_path).github.get_all_repos() == []


def test_setup_records_repo_logs_and_mode(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    log_file = tmp_path / "app.log"
    log_file.write_text("")

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "fix", "--base-branch", "develop",
        "--logs", str(log_file),
    ])
    assert result.exit_code == 0, result.output

    config = Config.load(config_path)
    repos = config.github.get_all_repos()
    assert repos[0].repo == "acme/webapp"
    assert repos[0].mode == "fix"
    assert repos[0].base_branch == "develop"
    assert config.monitor.log_files == [str(log_file)]


def test_setup_rejects_a_malformed_repo_without_aborting(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "not-a-repo",
    ])
    assert result.exit_code == 0, result.output
    assert "owner/name form" in result.output
    assert Config.load(config_path).github.get_all_repos() == []


def test_setup_rejects_an_unknown_provider(fake_keyring, api_key, tmp_path):
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--provider", "nonesuch",
    ])
    assert result.exit_code == 1
    assert "Unknown provider" in result.output


def test_setup_is_idempotent(fake_keyring, api_key, no_git_detect, tmp_path):
    """Re-running must not duplicate repos or lose settings."""
    config_path = tmp_path / "config.toml"
    args = [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "fix",
    ]
    runner.invoke(app, args)
    runner.invoke(app, args)

    config = Config.load(config_path)
    assert len(config.github.get_all_repos()) == 1
    assert config.github.get_all_repos()[0].mode == "fix"


def test_setup_preserves_a_second_repo(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/first",
    ])
    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/second",
    ])

    repos = [rc.repo for rc in Config.load(config_path).github.get_all_repos()]
    assert repos == ["acme/first", "acme/second"]


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------


def test_github_actions_reuses_the_stored_github_token(fake_keyring, api_key, tmp_path):
    """Nobody should have to paste a second token for the same account."""
    config_path = tmp_path / "config.toml"
    AuthManager().set_github_token("ghp_stored")

    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--github-actions",
    ])
    config = Config.load(config_path)
    assert config.monitor.github_actions_repos == ["acme/webapp"]
    assert config.monitor.github_actions_token == "ghp_stored"


def test_github_actions_is_skipped_without_a_token(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--github-actions",
    ])
    assert "needs a GitHub token" in result.output
    assert Config.load(config_path).monitor.github_actions_repos == []


# ---------------------------------------------------------------------------
# Closing summary
# ---------------------------------------------------------------------------


def test_summary_points_at_local_reports_without_a_repo(
    fake_keyring, api_key, no_git_detect, tmp_path
):
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--logs", str(log_file),
    ])
    assert "local reports" in result.output


def test_summary_points_at_prs_once_a_repo_is_set(fake_keyring, api_key, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    AuthManager().set_github_token("ghp_stored")
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--repo", "acme/webapp", "--logs", str(log_file),
    ])
    assert "--dry-run" in result.output
