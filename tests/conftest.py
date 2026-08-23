import getpass

import keyring
import keyring.errors
import pytest

from maajun.agent.core import Agent
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import CompletionResponse, ProviderError
from maajun.providers.factory import ProviderFactory


@pytest.fixture(autouse=True)
def getpass_reads_stdin(monkeypatch):
    """In a real terminal getpass reads /dev/tty, bypassing the test
    runner's scripted stdin — tests would block waiting for the keyboard
    (and capture whatever the developer types). Force plain stdin."""
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": input(prompt))


@pytest.fixture(autouse=True)
def cli_output_is_uncolored(monkeypatch):
    """Rich calls itself a terminal when it sees GITHUB_ACTIONS, so that build
    logs come out colored. That styles each option name in two spans, turning
    "--once" into "-\x1b[0m\x1b[1;36m-once" — a substring assertion on any
    rendered CLI text then fails on CI and nowhere else. Keep it plain."""
    for var in ("GITHUB_ACTIONS", "FORCE_COLOR", "TF_BUILD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def never_the_real_home(monkeypatch, tmp_path):
    """No test may reach the config or data directory maajun actually uses.

    `reset` deletes whatever daemon.workdir resolves to, and a test config
    with no [daemon] section resolved to the real one — so running the suite
    deleted the incident database, the clones, and the reports.
    """
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


@pytest.fixture(autouse=True)
def no_github_cli(monkeypatch):
    """No test borrows the developer's own gh login or SSH keys.

    maajun falls back to `gh auth token` when the keyring is empty, and
    probes SSH during setup — so without this a machine with gh logged in
    takes different paths than CI, and every setup test waits on ssh.
    """
    import importlib

    stubs = {
        "gh_token": lambda: "",
        "gh_available": lambda: False,
        "gh_account": lambda: "",
        "gh_login": lambda: 1,
        "ssh_works": lambda: False,
        # Reaches the real keyring and the GitHub API otherwise, which also
        # decides whether a bare repo name can be completed.
        "account_login": lambda token=None: "",
    }
    # Patched per module, since each imported the names it uses by value.
    for module_name in (
        "maajun.auth",
        "maajun.cli.setup",
        "maajun.cli.github_auth",
        "maajun.cli.monitor",
        "maajun.vcs.gh",
    ):
        module = importlib.import_module(module_name)
        for name, value in stubs.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)


# ---------------------------------------------------------------------------
# Keyring mock
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_keyring(monkeypatch):
    """In-memory keyring that satisfies AuthManager tests."""
    store = {}

    def delete_password(service, name):
        if (service, name) not in store:
            raise keyring.errors.PasswordDeleteError(name)
        del store[(service, name)]

    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get((s, n)))
    monkeypatch.setattr(keyring, "set_password", lambda s, n, v: store.__setitem__((s, n), v))
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return store


# ---------------------------------------------------------------------------
# Fake AI provider
# ---------------------------------------------------------------------------


class FakeProvider:
    """Deterministic provider for unit tests that don't hit the API."""

    def __init__(self, reply="pong", fail=False):
        self.reply = reply
        self.fail = fail
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.last_messages = list(messages)
        if self.fail:
            raise ProviderError("boom")
        return CompletionResponse(content=self.reply, thinking="hmm ")

    async def stream_completion(self, messages, **kwargs):
        self.last_messages = list(messages)
        if self.fail:
            raise ProviderError("boom")
        yield "thinking", "hmm "
        yield "content", self.reply[: len(self.reply) // 2]
        yield "content", self.reply[len(self.reply) // 2 :]

    async def validate_credentials(self):
        return not self.fail

    def get_provider_name(self):
        return "deepseek"


@pytest.fixture
def default_provider() -> str:
    """The provider setup picks when the user says nothing.

    A fixture rather than a constant to import: `tests` is not a package, so
    `from tests.conftest import ...` only resolves when the repo root happens
    to be on sys.path — true under `python -m pytest`, false under the
    `pytest` console script CI runs.
    """
    return AIProviderConfig().provider


@pytest.fixture
def fake_provider():
    return FakeProvider()


@pytest.fixture
def agent(monkeypatch, fake_provider):
    """Agent wired to a FakeProvider — no API calls."""
    monkeypatch.setattr(ProviderFactory, "create_provider", lambda *a, **k: fake_provider)
    return Agent(Config(ai=AIProviderConfig(provider="deepseek", api_key="x")))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def default_config():
    return Config()
