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
    auth.clear_all()
    assert auth.get_all_providers_with_keys() == []


# ---------------------------------------------------------------------------
# GitHub token: the keyring, and nothing else
# ---------------------------------------------------------------------------


def test_github_token_round_trips_through_the_keyring(fake_keyring):
    auth = AuthManager()
    auth.set_github_token("  ghp_stored  ")
    assert auth.get_github_token() == "ghp_stored"
    assert auth.has_github_token() is True


def test_no_stored_token_means_no_token(fake_keyring):
    auth = AuthManager()
    assert auth.get_github_token() is None
    assert auth.has_github_token() is False


def test_github_token_ignores_the_environment(fake_keyring, monkeypatch):
    """Credentials come from the keyring only — $GITHUB_TOKEN is not consulted.

    One source means `status` can never report a token the daemon won't use.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    auth = AuthManager()
    auth.set_github_token("keyring_token")
    assert auth.get_github_token() == "keyring_token"


def test_no_stored_token_is_not_rescued_by_the_environment(fake_keyring, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    assert AuthManager().get_github_token() is None


def test_api_key_ignores_the_environment(fake_keyring, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    auth = AuthManager()
    assert auth.get_api_key("deepseek") is None
    assert auth.has_api_key("deepseek") is False
    auth.set_api_key("deepseek", "sk-stored")
    assert auth.get_api_key("deepseek") == "sk-stored"


def test_a_logged_in_gh_cli_is_not_borrowed_from(fake_keyring, monkeypatch):
    """maajun never shells out to `gh auth token`.

    Borrowing another tool's credential means the token maajun pushes with can
    change without maajun being told, so `status` could not vouch for it. The
    assertion is that no subprocess runs at all.
    """
    def explode(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("maajun must not shell out for credentials")

    monkeypatch.setattr("subprocess.run", explode)
    monkeypatch.setattr("shutil.which", explode)

    assert AuthManager().get_github_token() is None


def test_clearing_the_token_leaves_nothing_behind(fake_keyring):
    auth = AuthManager()
    auth.set_github_token("ghp_stored")
    auth.clear_github_token()
    assert auth.get_github_token() is None


# ---------------------------------------------------------------------------
# Borrowing the GitHub CLI's login
# ---------------------------------------------------------------------------


def test_the_gh_login_stands_in_for_a_stored_token(fake_keyring, monkeypatch):
    """A machine where someone ran `gh auth login` needs no second credential."""
    monkeypatch.setattr("maajun.auth.gh_token", lambda: "gho_from_gh")
    auth = AuthManager()

    assert auth.get_github_token() == "gho_from_gh"
    assert auth.has_github_token()
    assert auth.github_token_source() == "gh"


def test_a_stored_token_wins_over_the_gh_login(fake_keyring, monkeypatch):
    monkeypatch.setattr("maajun.auth.gh_token", lambda: "gho_from_gh")
    auth = AuthManager()
    auth.set_github_token("ghp_mine")

    assert auth.get_github_token() == "ghp_mine"
    assert auth.github_token_source() == "keyring"


def test_gh_is_asked_once(fake_keyring, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "maajun.auth.gh_token", lambda: calls.append(1) or "gho_from_gh"
    )
    auth = AuthManager()

    auth.get_github_token()
    auth.get_github_token()

    assert len(calls) == 1


def test_signing_out_does_not_claim_to_undo_a_gh_login(fake_keyring, monkeypatch):
    """Clearing our own copy is ours to do; their gh session is not."""
    monkeypatch.setattr("maajun.auth.gh_token", lambda: "gho_from_gh")
    auth = AuthManager()
    auth.set_github_token("ghp_mine")

    auth.clear_github_token()

    assert auth.get_github_token() == "gho_from_gh"
