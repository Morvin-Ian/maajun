import json
import os
import stat
import sys
from pathlib import Path

import keyring
import keyring.errors

from maajun.config import default_config_path
from maajun.providers.base import ProviderType
from maajun.vcs.gh import gh_token

SERVICE_NAME = "maajun"
GITHUB_KEY_NAME = "github_token"

# Readable and writable by its owner, nobody else. The directory too: a
# world-executable parent lets anyone stat their way to the file.
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
DIR_MODE = stat.S_IRWXU


def credentials_file() -> Path:
    """Where credentials go when there is no keyring to put them in."""
    return default_config_path().parent / "credentials.json"


def keyring_works() -> bool:
    """Whether this machine has a keyring that can actually hold a secret.

    A headless server usually does not, and finding that out *after* someone
    has typed a key is how a secret gets thrown away.
    """
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    if type(backend).__module__.endswith("fail"):
        return False
    try:
        keyring.set_password(SERVICE_NAME, "probe", "probe")
        keyring.delete_password(SERVICE_NAME, "probe")
    except Exception:
        return False
    return True


def read_file_store() -> dict[str, str]:
    try:
        data = json.loads(credentials_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_file_store(values: dict[str, str]) -> None:
    """Replace the file, never widening its permissions."""
    path = credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, DIR_MODE)
    # Created empty at the right mode first: writing then chmod'ing leaves a
    # window where the secret is on disk world-readable.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(handle, "w") as f:
        json.dump(values, f, indent=2)
    os.chmod(path, FILE_MODE)


def file_store_enabled() -> bool:
    return credentials_file().exists()


def enable_file_store() -> None:
    """Start keeping credentials in a file, for a host with no keyring."""
    if not file_store_enabled():
        write_file_store({})


def get_stored(name: str) -> str | None:
    """A stored secret: the keyring first, then the file if there is one."""
    try:
        value = keyring.get_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
        value = None
    return value or read_file_store().get(name) or None


def set_stored(name: str, value: str) -> None:
    """Store a secret where this machine can keep it.

    The keyring when there is one. Otherwise the file, but only once someone
    has said to use it — silently writing a secret to disk because the
    keyring was missing is not a decision to make on their behalf.
    """
    try:
        keyring.set_password(SERVICE_NAME, name, value)
        return
    except keyring.errors.KeyringError as e:
        if not file_store_enabled():
            raise RuntimeError(
                "No usable keyring on this machine (normal on a server). "
                "Run `maajun setup` and choose where to keep credentials, "
                f"or install a keyring backend: {e}"
            ) from e
    values = read_file_store()
    values[name] = value
    write_file_store(values)


def delete_stored(name: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except keyring.errors.KeyringError:
        pass
    values = read_file_store()
    if values.pop(name, None) is not None:
        write_file_store(values)


def install_backend_command() -> str:
    """How to add a keyring backend, for the way maajun was installed."""
    prefix = sys.prefix
    if "/pipx/" in prefix or "\\pipx\\" in prefix:
        return "pipx inject maajun keyrings.alt"
    if "/uv/tools/" in prefix:
        return "uv tool install maajun --with keyrings.alt"
    return f"{Path(sys.executable).name} -m pip install keyrings.alt"


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

        key = get_stored(key_name)
        if key:
            self.cache[provider] = key
        return key

    def set_api_key(self, provider: str, key: str) -> None:
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            raise ValueError(f"Unsupported provider: {provider}")

        key = key.strip()
        set_stored(key_name, key)
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
        delete_stored(key_name)
        self.cache.pop(provider, None)

    def get_github_token(self) -> str | None:
        """The token for the GitHub API: the keyring, else a `gh` login.

        Borrowing gh's token means a machine where someone has run
        `gh auth login` needs no second credential, and maajun still stores
        nothing of its own.
        """
        stored = get_stored(GITHUB_KEY_NAME)
        if stored:
            return stored
        if self.gh_token is None:
            self.gh_token = gh_token()
        return self.gh_token or None

    def github_token_source(self) -> str:
        """Where the token comes from: "keyring", "file", "gh", or ""."""
        try:
            if keyring.get_password(SERVICE_NAME, GITHUB_KEY_NAME):
                return "keyring"
        except keyring.errors.KeyringError:
            pass
        if read_file_store().get(GITHUB_KEY_NAME):
            return "file"
        return "gh" if self.get_github_token() else ""

    def set_github_token(self, token: str) -> None:
        set_stored(GITHUB_KEY_NAME, token.strip())

    def has_github_token(self) -> bool:
        return self.get_github_token() is not None

    def clear_github_token(self) -> None:
        delete_stored(GITHUB_KEY_NAME)
        # Only maajun's own copy is cleared; a gh login is not ours to undo.
        self.gh_token = None

    def clear_all(self) -> None:
        for provider in self.SUPPORTED_PROVIDERS:
            self.clear_provider_key(provider)
        self.clear_github_token()
        self.cache = {}
