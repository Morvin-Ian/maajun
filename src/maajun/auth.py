import os
import shutil
import subprocess

import keyring
import keyring.errors

from maajun.providers.base import ProviderType

SERVICE_NAME = "maajun"
GITHUB_KEY_NAME = "github_token"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

# Distinguishes "not looked up yet" from a cached "gh has no token".
_UNSET = object()


def _keyring_get(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
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
        self._gh_cli_token: str | None | object = _UNSET

    @staticmethod
    def _provider_env_var(provider: str) -> str:
        return f"{provider.upper()}_API_KEY"

    def get_api_key(self, provider: str) -> str | None:
        """Get API key for a provider: env var first, then keyring."""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return None

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


    def _token_from_gh_cli(self) -> str | None:
        """Borrow the token from a logged-in `gh` CLI, if one is installed.

        Cached for the process lifetime: get_github_token() is called from
        status checks and daemon startup, and forking gh on every call (with a
        10s timeout each) is far too expensive to repeat.
        """
        if self._gh_cli_token is not _UNSET:
            return self._gh_cli_token

        token: str | None = None
        if shutil.which("gh"):
            try:
                result = subprocess.run(
                    ["gh", "auth", "token"],
                    capture_output=True, text=True, check=True, timeout=10,
                )
                token = result.stdout.strip() or None
            except (subprocess.SubprocessError, OSError):
                # Not logged in, gh broken, or hung past the timeout —
                # SubprocessError covers CalledProcessError and TimeoutExpired.
                token = None
        self._gh_cli_token = token
        return token

    def github_token_source(self) -> str | None:
        if _keyring_get(GITHUB_KEY_NAME):
            return "keyring"
        if self._token_from_gh_cli():
            return "gh"
        return None

    def get_github_token(self) -> str | None:
        token = _keyring_get(GITHUB_KEY_NAME)
        if token:
            return token
        return self._token_from_gh_cli()

    def set_github_token(self, token: str) -> None:
        _keyring_set(GITHUB_KEY_NAME, token.strip())

    def has_github_token(self) -> bool:
        return self.get_github_token() is not None

    def clear_github_token(self) -> None:
        _keyring_delete(GITHUB_KEY_NAME)
        self._gh_cli_token = _UNSET

    def clear_all(self) -> None:
        for provider in self.SUPPORTED_PROVIDERS:
            self.clear_provider_key(provider)
        self.clear_github_token()
        self._cache = {}
        self._gh_cli_token = _UNSET
