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


def test_login_stores_key(fake_keyring, monkeypatch):
    """login with a single configured provider auto-selects it."""
    # No providers have keys yet — login should prompt for provider choice
    # and then accept the key.
    result = runner.invoke(app, ["login"], input="1\nsk-test-key\n")
    assert result.exit_code == 0
    assert "saved" in result.output.lower() or "key" in result.output.lower()


def test_login_rejects_empty_key(fake_keyring):
    result = runner.invoke(app, ["login"], input="1\n\n")
    assert result.exit_code == 1
    assert "No key entered" in result.output


def test_login_overwrite_prompt(fake_keyring):
    """When a key already exists, login asks to overwrite."""
    from maajun.auth import AuthManager

    auth = AuthManager()
    auth.set_api_key("deepseek", "old-key")

    result = runner.invoke(app, ["login"], input="1\ny\nnew-key\n")
    assert result.exit_code == 0


def test_login_cancel_on_no_overwrite(fake_keyring):
    from maajun.auth import AuthManager

    auth = AuthManager()
    auth.set_api_key("deepseek", "old-key")

    result = runner.invoke(app, ["login"], input="1\nn\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output


# ---------------------------------------------------------------------------
# provider-list
# ---------------------------------------------------------------------------


def test_provider_list(fake_keyring):
    result = runner.invoke(app, ["provider-list"])
    assert result.exit_code == 0
    assert "deepseek" in result.output.lower()
    assert "openai" in result.output.lower()
    assert "anthropic" in result.output.lower()


# ---------------------------------------------------------------------------
# config-set-key
# ---------------------------------------------------------------------------


def test_config_set_key_interactive(fake_keyring):
    result = runner.invoke(app, ["config-set-key", "deepseek"], input="sk-test\n")
    assert result.exit_code == 0
    assert "saved" in result.output.lower()


def test_config_set_key_cli_arg(fake_keyring):
    result = runner.invoke(app, ["config-set-key", "deepseek", "sk-cli-test"])
    assert result.exit_code == 0
    assert "saved" in result.output.lower()
    # Should warn about shell history
    assert "history" in result.output.lower()


def test_config_set_key_empty_input(fake_keyring):
    result = runner.invoke(app, ["config-set-key", "deepseek"], input="\n")
    assert result.exit_code == 1
    assert "No key entered" in result.output


# ---------------------------------------------------------------------------
# config-remove-key
# ---------------------------------------------------------------------------


def test_config_remove_key(fake_keyring):
    from maajun.auth import AuthManager

    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")

    result = runner.invoke(app, ["config-remove-key", "deepseek"])
    assert result.exit_code == 0
    assert "removed" in result.output.lower()


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


def test_init_writes_config(tmp_path):
    config_path = tmp_path / "config.toml"
    # Non-interactive mode
    result = runner.invoke(app, ["init", "--config", str(config_path), "--no-interactive"])
    assert result.exit_code == 0
    assert config_path.exists()
    content = config_path.read_text()
    assert "[ai]" in content
    assert "[github]" in content
    assert "[monitor]" in content


def test_init_refuses_overwrite(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("existing")
    result = runner.invoke(app, ["init", "--config", str(config_path)], input="n\n")
    assert result.exit_code == 0
    assert config_path.read_text() == "existing"


def test_init_overwrites_on_confirm(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("old")
    # Non-interactive mode with overwrite confirmation
    result = runner.invoke(
        app, ["init", "--config", str(config_path), "--no-interactive"], input="y\n"
    )
    assert result.exit_code == 0
    assert "[ai]" in config_path.read_text()


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def test_watch_fails_without_github_token(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "No GitHub token" in result.output


def test_watch_fails_without_repo(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    config_path = tmp_path / "config.toml"
    config_path.write_text("[github]\n")
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "github.repo" in result.output


# ---------------------------------------------------------------------------
# chat (non-interactive, provider not configured)
# ---------------------------------------------------------------------------


def test_chat_fails_without_providers(fake_keyring):
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 1
    assert "No providers configured" in result.output


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


def test_root_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Maajun" in result.output


def test_chat_help():
    result = runner.invoke(app, ["chat", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output or "provider" in result.output.lower()


def test_watch_help():
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output
    assert "--dry-run" in result.output
    assert "--verbose" in result.output


def test_init_help():
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output


# ---------------------------------------------------------------------------
# watch --dry-run
# ---------------------------------------------------------------------------


def test_watch_dry_run_fails_without_github_token(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--dry-run", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "No GitHub token" in result.output


# ---------------------------------------------------------------------------
# github-login
# ---------------------------------------------------------------------------


class _FakeGitHubClient:
    def __init__(self, token, **kwargs):
        self.token = token

    async def validate_token(self):
        return "morvin"

    async def can_push(self, repo):
        return True

    async def aclose(self):
        pass


def test_github_login_sets_repo_and_token(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.setup.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="Morvin-Ian/maajun\nghp_token\n1\n",  # repo, token, mode (1=suggest)
    )

    assert result.exit_code == 0
    assert 'repo = "Morvin-Ian/maajun"' in config_path.read_text()
    assert fake_keyring[("maajun", "github_token")] == "ghp_token"


def test_github_login_keeps_configured_repo_on_empty_input(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.setup.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/real"\n')

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="\nghp_token\n1\n",  # empty (keep repo), token, mode (1=suggest)
    )

    assert result.exit_code == 0
    assert "owner/real" in result.output
    assert 'repo = "owner/real"' in config_path.read_text()


def test_github_login_treats_placeholder_repo_as_unset(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.setup.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n')

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="\n",
    )

    assert result.exit_code == 1
    assert "No repository entered" in result.output


def test_github_login_rejects_bad_repo_format(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.setup.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="not-a-repo\n",
    )

    assert result.exit_code == 1
    assert "owner/name form" in result.output


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


def test_config_show_masks_secrets(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[daemon]\n[daemon.email]\npassword = \"hunter2\"\n")
    result = runner.invoke(
        app, ["config", "daemon.email.password", "--config", str(config_path)]
    )
    assert result.exit_code == 0
    assert "hunter2" not in result.output
    assert "***" in result.output


def test_status_reports_missing_credentials(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\n[monitor]\nlog_files = []\n')

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 1  # missing API key + token
    assert "maajun login" in result.output
    assert "github-login" in result.output.lower() or "GITHUB_TOKEN" in result.output


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
