import keyring
import keyring.errors
import pytest

from maajun.auth import AuthManager


@pytest.fixture
def fake_keyring(monkeypatch):
    store = {}

    def delete_password(service, name):
        if (service, name) not in store:
            raise keyring.errors.PasswordDeleteError(name)
        del store[(service, name)]

    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get((s, n)))
    monkeypatch.setattr(keyring, "set_password", lambda s, n, v: store.__setitem__((s, n), v))
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return store


def test_set_and_get_key(fake_keyring):
    auth = AuthManager()
    auth.set_api_key("deepseek", "  sk-test  ")
    assert auth.get_api_key("deepseek") == "sk-test"
    assert auth.has_api_key("deepseek")


def test_unknown_provider_rejected(fake_keyring):
    auth = AuthManager()
    with pytest.raises(ValueError):
        auth.set_api_key("groq", "sk-test")
    assert auth.get_api_key("groq") is None


def test_supported_providers_match_provider_type(fake_keyring):
    from maajun.providers.base import ProviderType

    assert set(AuthManager.SUPPORTED_PROVIDERS) == {p.value for p in ProviderType}


def test_clear_provider_key_is_idempotent(fake_keyring):
    auth = AuthManager()
    auth.clear_provider_key("deepseek")  # nothing stored — must not raise
    auth.set_api_key("deepseek", "sk-test")
    auth.clear_provider_key("deepseek")
    assert not auth.has_api_key("deepseek")


def test_clear_all(fake_keyring):
    auth = AuthManager()
    auth.set_api_key("deepseek", "a")
    auth.set_api_key("openai", "b")
    auth.clear_all()
    assert auth.get_all_providers_with_keys() == []


# ---------------------------------------------------------------------------
# GitHub token resolution: env -> keyring -> gh CLI
# ---------------------------------------------------------------------------


def _fake_gh(monkeypatch, *, installed=True, result=None, raises=None):
    import subprocess as sp

    monkeypatch.setattr(
        "maajun.auth.shutil.which", lambda name: "/usr/bin/gh" if installed else None
    )
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if raises is not None:
            raise raises
        return sp.CompletedProcess(cmd, 0, stdout=result or "", stderr="")

    monkeypatch.setattr("maajun.auth.subprocess.run", run)
    return calls


def test_github_token_prefers_env_over_keyring(fake_keyring, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    auth = AuthManager()
    auth.set_github_token("keyring_token")
    assert auth.get_github_token() == "env_token"
    assert auth.github_token_source() == "env"


def test_github_token_falls_back_to_keyring(fake_keyring, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _fake_gh(monkeypatch, installed=False)
    auth = AuthManager()
    auth.set_github_token("keyring_token")
    assert auth.get_github_token() == "keyring_token"
    assert auth.github_token_source() == "keyring"


def test_github_token_falls_back_to_gh_cli(fake_keyring, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _fake_gh(monkeypatch, result="gho_from_cli\n")
    auth = AuthManager()
    assert auth.get_github_token() == "gho_from_cli"
    assert auth.github_token_source() == "gh"


def test_gh_cli_result_is_cached(fake_keyring, monkeypatch):
    """Startup and status both ask for the token; forking gh each time (with a
    10s timeout) is far too expensive to repeat."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = _fake_gh(monkeypatch, result="gho_from_cli")
    auth = AuthManager()
    for _ in range(5):
        auth.get_github_token()
    assert len(calls) == 1


def test_hung_gh_cli_does_not_crash(fake_keyring, monkeypatch):
    """Regression: TimeoutExpired is a SubprocessError, not an OSError or a
    CalledProcessError, so the original handler let it escape."""
    import subprocess as sp

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _fake_gh(monkeypatch, raises=sp.TimeoutExpired(cmd=["gh"], timeout=10))
    assert AuthManager().get_github_token() is None


def test_failed_gh_cli_does_not_crash(fake_keyring, monkeypatch):
    import subprocess as sp

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _fake_gh(monkeypatch, raises=sp.CalledProcessError(1, ["gh"]))
    assert AuthManager().get_github_token() is None


def test_no_gh_installed_reports_no_source(fake_keyring, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _fake_gh(monkeypatch, installed=False)
    auth = AuthManager()
    assert auth.get_github_token() is None
    assert auth.github_token_source() is None
