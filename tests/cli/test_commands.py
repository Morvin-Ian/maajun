"""Tests for the CLI commands (Typer app)."""

import keyring
import keyring.errors
import pytest
from typer.testing import CliRunner

from maajun.cli import app

runner = CliRunner()


@pytest.fixture
def fake_keyring(monkeypatch):
    """In-memory keyring for CLI tests."""
    store = {}

    def delete_password(service, name):
        if (service, name) not in store:
            raise keyring.errors.PasswordDeleteError(name)
        del store[(service, name)]

    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get((s, n)))
    monkeypatch.setattr(keyring, "set_password", lambda s, n, v: store.__setitem__((s, n), v))
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return store


class _FakeGitHubClient:
    def __init__(self, token, **kwargs):
        self.token = token

    async def validate_token(self):
        return "morvin"

    async def can_push(self, repo):
        return True

    async def aclose(self):
        pass


# ---------------------------------------------------------------------------
# Main callback (no subcommand)
# ---------------------------------------------------------------------------


def test_main_shows_welcome_when_no_providers(fake_keyring):
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Welcome to Maajun" in result.output or "Setup Required" in result.output


def test_main_shows_providers_when_configured(fake_keyring, monkeypatch):
    from maajun.auth import AuthManager

    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")

    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Configured" in result.output


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# provider-list
# ---------------------------------------------------------------------------


def test_provider_list(fake_keyring):
    result = runner.invoke(app, ["provider-list"])
    assert result.exit_code == 0
    assert "deepseek" in result.output.lower()


# ---------------------------------------------------------------------------
# config-set-key
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# config-remove-key
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# sign-out
# ---------------------------------------------------------------------------


def test_sign_out(fake_keyring):
    from maajun.auth import AuthManager

    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")

    result = runner.invoke(app, ["sign-out"])
    assert result.exit_code == 0
    assert "cleared" in result.output.lower()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def test_watch_fails_without_api_key(fake_keyring, tmp_path):
    """The API key is the one hard requirement."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "No API key" in result.output


def test_watch_fails_when_a_repo_is_set_but_no_token(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("maajun.auth.shutil.which", lambda name: None)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "no GitHub token" in result.output


def test_watch_without_a_repo_runs_in_local_mode(fake_keyring, tmp_path, monkeypatch):
    """GitHub is optional: with no repo, errors are analyzed into local reports."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[monitor]\nlog_files = ["{log_file}"]\n'
        f'[daemon]\nworkdir = "{tmp_path / "data"}"\nrepo_path = "{tmp_path}"\n'
    )
    result = runner.invoke(app, ["watch", "--once", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "local" in result.output
    assert str(tmp_path) in result.output


# ---------------------------------------------------------------------------
# chat (non-interactive, provider not configured)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


def test_root_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Maajun" in result.output


def test_watch_help():
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output
    assert "--dry-run" in result.output
    assert "--verbose" in result.output


# ---------------------------------------------------------------------------
# watch --dry-run
# ---------------------------------------------------------------------------


def test_watch_dry_run_still_requires_an_api_key(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--dry-run", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "No API key" in result.output


# ---------------------------------------------------------------------------
# config / add-repo / status commands
# ---------------------------------------------------------------------------


def test_config_set_persists_and_validates(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\nmode = "suggest"\n')

    result = runner.invoke(app, ["config", "github.mode", "fix", "--config", str(config_path)])
    assert result.exit_code == 0
    assert 'mode = "fix"' in config_path.read_text()

    bad = runner.invoke(app, ["config", "github.mode", "yolo", "--config", str(config_path)])
    assert bad.exit_code == 1
    assert 'mode = "fix"' in config_path.read_text()  # unchanged


def test_status_reports_missing_credentials(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n[monitor]\nlog_files = []\n')

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 1  # missing API key + token
    assert "maajun setup" in result.output
    assert "GITHUB_TOKEN" in result.output or "gh auth login" in result.output


def test_status_all_green(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.cli.monitor.GitHubClient", _FakeGitHubClient)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    logf = tmp_path / "app.log"
    logf.write_text("")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[ai]\nprovider = "deepseek"\n'
        f'[github]\nrepo = "owner/name"\n'
        f'[monitor]\nlog_files = ["{logf}"]\n'
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "Ready" in result.output


def test_add_repo_migrates_and_appends(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/first"\nmode = "suggest"\n')

    r1 = runner.invoke(
        app, ["add-repo", "owner/second", "-m", "fix", "--config", str(config_path)]
    )
    assert r1.exit_code == 0

    from maajun.config import Config

    cfg = Config.load(config_path)
    assert [rc.repo for rc in cfg.github.repos] == ["owner/first", "owner/second"]
    assert cfg.github.repo == ""  # legacy scalar cleared

    bad = runner.invoke(app, ["add-repo", "not-a-repo", "--config", str(config_path)])
    assert bad.exit_code == 1


def test_reset_removes_everything(fake_keyring, tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    # Env vars take precedence over the keyring — clear any that leaked in.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    from maajun.auth import AuthManager

    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")
    auth.set_github_token("ghp_test")

    (cfg_home / "maajun").mkdir(parents=True)
    (cfg_home / "maajun" / "config.toml").write_text('[github]\nrepo = "owner/name"\n')
    (data_home / "maajun").mkdir(parents=True)
    (data_home / "maajun" / "incidents.db").write_text("")

    result = runner.invoke(app, ["reset", "--force"])
    assert result.exit_code == 0
    assert not (cfg_home / "maajun").exists()
    assert not (data_home / "maajun").exists()
    # A fresh manager reflects the cleared keyring (the old instance caches).
    fresh = AuthManager()
    assert not fresh.has_api_key("deepseek")
    assert not fresh.has_github_token()


def test_reset_cancels_without_confirmation(fake_keyring, tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (cfg_home / "maajun").mkdir(parents=True)
    (cfg_home / "maajun" / "config.toml").write_text("[github]\n")

    result = runner.invoke(app, ["reset"], input="no\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    assert (cfg_home / "maajun").exists()  # nothing deleted
