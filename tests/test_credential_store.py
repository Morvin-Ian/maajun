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
    file_store_in_use,
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


def test_a_server_with_no_keyring_just_works(headless):
    """The alternative — failing, and sending someone to install a package
    that writes the same file — costs a wasted run and buys nothing."""
    AuthManager().set_api_key("deepseek", "sk-secret")

    assert AuthManager().get_api_key("deepseek") == "sk-secret"
    assert credentials_file().exists()


def test_credentials_round_trip(headless):
    auth = AuthManager()

    auth.set_api_key("deepseek", "sk-secret")
    auth.set_github_token("ghp_token")

    fresh = AuthManager()
    assert fresh.get_api_key("deepseek") == "sk-secret"
    assert fresh.get_github_token() == "ghp_token"
    assert fresh.github_token_source() == "file"


def test_the_file_is_readable_only_by_its_owner(headless):
    """It holds an API key in the clear; the mode is the whole protection."""
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
    AuthManager().set_api_key("deepseek", "sk-secret")

    assert seen and all(mode == 0o600 for mode in seen)


def test_signing_out_empties_the_file(headless):
    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-secret")
    auth.set_github_token("ghp_token")

    auth.clear_all()

    assert json.loads(credentials_file().read_text()) == {}
    assert AuthManager().get_api_key("deepseek") is None


def test_a_garbled_file_is_not_fatal(headless):
    credentials_file().parent.mkdir(parents=True, exist_ok=True)
    credentials_file().write_text("{ not json")

    assert AuthManager().get_api_key("deepseek") is None


def test_the_keyring_still_wins_where_there_is_one(fake_keyring, tmp_path, monkeypatch):
    """The file is a fallback for hosts without one, not a replacement."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    auth = AuthManager()

    auth.set_api_key("deepseek", "sk-in-keyring")

    assert not credentials_file().exists()
    assert auth.get_api_key("deepseek") == "sk-in-keyring"


def test_the_file_is_only_in_use_once_something_is_stored(headless):
    """`status` reports the location, so it must not claim one too early."""
    assert file_store_in_use() is False

    AuthManager().set_api_key("deepseek", "sk-secret")

    assert file_store_in_use() is True


def test_nowhere_to_write_is_still_an_error(headless, monkeypatch):
    """No keyring and no writable config directory: that one has to be said."""
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(auth_module, "write_file_store", refuse)

    with pytest.raises(RuntimeError, match="could not be written"):
        AuthManager().set_api_key("deepseek", "sk-secret")


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
