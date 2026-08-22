import keyring
import keyring.errors

from maajun.providers.base import ProviderType
from maajun.vcs.gh import gh_token

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
        # None until asked: shelling out to gh is worth doing once.
        self.gh_token: str | None = None

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
        """The token for the GitHub API: the keyring, else a `gh` login.

        Borrowing gh's token means a machine where someone has run
        `gh auth login` needs no second credential, and maajun still stores
        nothing of its own.
        """
        stored = get_keyring(GITHUB_KEY_NAME)
        if stored:
            return stored
        if self.gh_token is None:
            self.gh_token = gh_token()
        return self.gh_token or None

    def github_token_source(self) -> str:
        """Where the token comes from: "keyring", "gh", or "" — for status."""
        if get_keyring(GITHUB_KEY_NAME):
            return "keyring"
        return "gh" if self.get_github_token() else ""

    def set_github_token(self, token: str) -> None:
        set_keyring(GITHUB_KEY_NAME, token.strip())

    def has_github_token(self) -> bool:
        return self.get_github_token() is not None

    def clear_github_token(self) -> None:
        delete_keyring(GITHUB_KEY_NAME)
        # Only maajun's own copy is cleared; a gh login is not ours to undo.
        self.gh_token = None

    def clear_all(self) -> None:
        for provider in self.SUPPORTED_PROVIDERS:
            self.clear_provider_key(provider)
        self.clear_github_token()
        self.cache = {}
