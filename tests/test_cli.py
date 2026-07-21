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
    result = runner.invoke(app, ["init", "--config", str(config_path)])
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
    result = runner.invoke(app, ["init", "--config", str(config_path)], input="y\n")
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


def test_github_login_sets_repo_and_token(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="Morvin-Ian/maajun\nghp_token\n",
    )

    assert result.exit_code == 0
    assert 'repo = "Morvin-Ian/maajun"' in config_path.read_text()
    assert fake_keyring[("maajun", "github_token")] == "ghp_token"


def test_github_login_keeps_configured_repo_on_empty_input(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/real"\n')

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="\nghp_token\n",
    )

    assert result.exit_code == 0
    assert "owner/real" in result.output
    assert 'repo = "owner/real"' in config_path.read_text()


def test_github_login_treats_placeholder_repo_as_unset(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.vcs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("maajun.cli.GitHubClient", _FakeGitHubClient)
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
    monkeypatch.setattr("maajun.cli.GitHubClient", _FakeGitHubClient)
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["github-login", "--config", str(config_path)],
        input="not-a-repo\n",
    )

    assert result.exit_code == 1
    assert "owner/name form" in result.output


# ---------------------------------------------------------------------------
# _save_repo_to_config
# ---------------------------------------------------------------------------


def test_save_repo_replaces_line_and_keeps_comments(tmp_path):
    from maajun.cli import _save_repo_to_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[github]\n"
        "# Repository the daemon opens PRs against.\n"
        'repo = "owner/name"\n'
        'base_branch = "main"\n'
    )

    _save_repo_to_config(config_path, "me/app")

    content = config_path.read_text()
    assert 'repo = "me/app"' in content
    assert "# Repository the daemon opens PRs against." in content
    assert 'base_branch = "main"' in content


def test_save_repo_adds_line_to_existing_github_section(tmp_path):
    from maajun.cli import _save_repo_to_config

    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nbase_branch = "main"\n')

    _save_repo_to_config(config_path, "me/app")

    content = config_path.read_text()
    assert '[github]\nrepo = "me/app"' in content
    assert 'base_branch = "main"' in content


def test_save_repo_creates_config_from_template(tmp_path):
    from maajun.cli import _save_repo_to_config

    config_path = tmp_path / "config.toml"
    _save_repo_to_config(config_path, "me/app")

    content = config_path.read_text()
    assert 'repo = "me/app"' in content
    assert "owner/name" not in content
    assert "[monitor]" in content
