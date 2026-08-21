import keyring
import keyring.errors

from maajun.providers.base import ProviderType

SERVICE_NAME = "maajun"
GITHUB_KEY_NAME = "github_token"


def get_keyring(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
        return None


def set_keyring(name: str, value: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, name, value)
    except keyring.errors.KeyringError as e:
        raise RuntimeError(
            "No usable keyring backend (common on headless servers). "
            "maajun stores credentials only in the OS keyring, so install a "
            f"backend (e.g. keyrings.alt, or gnome-keyring) and retry: {e}"
        ) from e


def delete_keyring(name: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
        pass


class AuthManager:

    SUPPORTED_PROVIDERS: dict[str, str] = {
        p.value: f"{p.value}_api_key" for p in ProviderType
    }

    def __init__(self):
        self.cache = {}

    def get_api_key(self, provider: str) -> str | None:
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return None

        if provider in self.cache:
            return self.cache[provider]

        key = get_keyring(key_name)
        if key:
            self.cache[provider] = key
        return key

    def set_api_key(self, provider: str, key: str) -> None:
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            raise ValueError(f"Unsupported provider: {provider}")

        key = key.strip()
        set_keyring(key_name, key)
        self.cache[provider] = key

    def has_api_key(self, provider: str) -> bool:
        return self.get_api_key(provider) is not None

    def get_all_providers_with_keys(self) -> list[str]:
        return [
            provider for provider in self.SUPPORTED_PROVIDERS
            if self.has_api_key(provider)
        ]

    def clear_provider_key(self, provider: str) -> None:
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return
        delete_keyring(key_name)
        self.cache.pop(provider, None)

    def get_github_token(self) -> str | None:
        return get_keyring(GITHUB_KEY_NAME)

    def set_github_token(self, token: str) -> None:
        set_keyring(GITHUB_KEY_NAME, token.strip())

    def has_github_token(self) -> bool:
        return self.get_github_token() is not None

    def clear_github_token(self) -> None:
        delete_keyring(GITHUB_KEY_NAME)

    def clear_all(self) -> None:
        for provider in self.SUPPORTED_PROVIDERS:
            self.clear_provider_key(provider)
        self.clear_github_token()
        self.cache = {}
