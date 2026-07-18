import os

import keyring
import keyring.errors

from maajun.providers.base import ProviderType

SERVICE_NAME = "maajun"
GITHUB_KEY_NAME = "github_token"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"


def _keyring_get(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
        # Headless machines often have no keyring backend; fall back to env.
        return None


def _keyring_set(name: str, value: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, name, value)
    except keyring.errors.KeyringError as e:
        raise RuntimeError(
            "No usable keyring backend (common on headless servers). "
            f"Set the secret via an environment variable instead: {e}"
        ) from e


def _keyring_delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
        pass


class AuthManager:

    SUPPORTED_PROVIDERS: dict[str, str] = {
        p.value: f"{p.value}_api_key" for p in ProviderType
    }

    def __init__(self):
        self._cache = {}

    @staticmethod
    def _provider_env_var(provider: str) -> str:
        return f"{provider.upper()}_API_KEY"

    def get_api_key(self, provider: str) -> str | None:
        """Get API key for a provider: env var first, then keyring."""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return None

        env_key = os.environ.get(self._provider_env_var(provider), "").strip()
        if env_key:
            return env_key

        if provider in self._cache:
            return self._cache[provider]

        key = _keyring_get(key_name)
        if key:
            self._cache[provider] = key
        return key

    def set_api_key(self, provider: str, key: str) -> None:
        """Set API key for a provider"""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            raise ValueError(f"Unsupported provider: {provider}")

        key = key.strip()
        _keyring_set(key_name, key)
        self._cache[provider] = key

    def has_api_key(self, provider: str) -> bool:
        """Check if provider has an API key set"""
        return self.get_api_key(provider) is not None

    def get_all_providers_with_keys(self) -> list[str]:
        """Get all providers that have API keys set"""
        return [
            provider for provider in self.SUPPORTED_PROVIDERS
            if self.has_api_key(provider)
        ]

    def clear_provider_key(self, provider: str) -> None:
        """Clear API key for a provider"""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return
        _keyring_delete(key_name)
        self._cache.pop(provider, None)

    # -- GitHub -------------------------------------------------------

    def get_github_token(self) -> str | None:
        """GitHub token: GITHUB_TOKEN env var first, then keyring."""
        env_token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
        if env_token:
            return env_token
        return _keyring_get(GITHUB_KEY_NAME)

    def set_github_token(self, token: str) -> None:
        _keyring_set(GITHUB_KEY_NAME, token.strip())

    def has_github_token(self) -> bool:
        return self.get_github_token() is not None

    def clear_github_token(self) -> None:
        _keyring_delete(GITHUB_KEY_NAME)

    def clear_all(self) -> None:
        """Clear all stored credentials"""
        for provider in self.SUPPORTED_PROVIDERS:
            self.clear_provider_key(provider)
        self.clear_github_token()
        self._cache = {}
