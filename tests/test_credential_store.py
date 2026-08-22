import json
import os
import stat

import keyring
import keyring.backends.fail
import pytest

from maajun import auth as auth_module
from maajun.auth import (
    AuthManager,
    credentials_file,
    enable_file_store,
    file_store_enabled,
    install_backend_command,
    keyring_works,
)


@pytest.fixture
def headless(monkeypatch, tmp_path):
    """A machine with no usable keyring — a plain server."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(keyring, "get_keyring", keyring.backends.fail.Keyring)
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(
            keyring, name,
            lambda *a, **k: (_ for _ in ()).throw(
                keyring.errors.NoKeyringError("no backend")
            ),
        )
    return tmp_path


def test_a_server_with_no_keyring_is_recognised(headless):
    assert keyring_works() is False


def test_a_secret_is_not_written_to_disk_unasked(headless):
    """Falling back silently would downgrade everyone's security to a file."""
    with pytest.raises(RuntimeError, match="No usable keyring"):
        AuthManager().set_api_key("deepseek", "sk-secret")

    assert not credentials_file().exists()


def test_once_asked_for_credentials_round_trip(headless):
    enable_file_store()
    auth = AuthManager()

    auth.set_api_key("deepseek", "sk-secret")
    auth.set_github_token("ghp_token")

    fresh = AuthManager()
    assert fresh.get_api_key("deepseek") == "sk-secret"
    assert fresh.get_github_token() == "ghp_token"
    assert fresh.github_token_source() == "file"


def test_the_file_is_readable_only_by_its_owner(headless):
    """It holds an API key in the clear; the mode is the whole protection."""
    enable_file_store()
    AuthManager().set_api_key("deepseek", "sk-secret")

    path = credentials_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_the_file_is_never_briefly_world_readable(headless, monkeypatch):
    """Written at the right mode, not written and then chmod'ed."""
    seen = []
    real_open = os.open

    def watched(path, flags, mode=0o777):
        if str(path).endswith("credentials.json"):
            seen.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", watched)
    enable_file_store()
    AuthManager().set_api_key("deepseek", "sk-secret")

    assert seen and all(mode == 0o600 for mode in seen)


def test_signing_out_empties_the_file(headless):
    enable_file_store()
    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-secret")
    auth.set_github_token("ghp_token")

    auth.clear_all()

    assert json.loads(credentials_file().read_text()) == {}
    assert AuthManager().get_api_key("deepseek") is None


def test_a_garbled_file_is_not_fatal(headless):
    enable_file_store()
    credentials_file().write_text("{ not json")

    assert AuthManager().get_api_key("deepseek") is None


def test_the_keyring_still_wins_where_there_is_one(fake_keyring, tmp_path, monkeypatch):
    """The file is a fallback for hosts without one, not a replacement."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    auth = AuthManager()

    auth.set_api_key("deepseek", "sk-in-keyring")

    assert not credentials_file().exists()
    assert auth.get_api_key("deepseek") == "sk-in-keyring"


def test_the_file_store_is_remembered(headless):
    """So a later `maajun login` does not have to ask again."""
    assert file_store_enabled() is False

    enable_file_store()

    assert file_store_enabled() is True


@pytest.mark.parametrize("prefix,expected", [
    ("/home/u/.local/pipx/venvs/maajun", "pipx inject maajun keyrings.alt"),
    ("/home/u/.local/share/uv/tools/maajun", "uv tool install maajun --with keyrings.alt"),
])
def test_the_install_command_matches_how_maajun_was_installed(
    monkeypatch, prefix, expected
):
    """"pip install keyrings.alt" does nothing for a pipx install: it lands in
    the wrong environment, and the next run fails the same way."""
    monkeypatch.setattr(auth_module.sys, "prefix", prefix)

    assert install_backend_command() == expected


def test_an_ordinary_environment_is_told_to_pip_install(monkeypatch):
    monkeypatch.setattr(auth_module.sys, "prefix", "/usr")

    assert "pip install keyrings.alt" in install_backend_command()
