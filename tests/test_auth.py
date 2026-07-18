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
