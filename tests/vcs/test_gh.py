import pytest

from maajun.utils.commands import CommandOutput
from maajun.vcs.gh import gh_token, remote_url, ssh_works


@pytest.fixture
def command(monkeypatch):
    def install(output):
        monkeypatch.setattr(
            "maajun.vcs.gh.run_text", lambda cmd, timeout=30.0: output
        )

    return install


def test_the_token_comes_back_stripped(command):
    command(CommandOutput(stdout="gho_abc123\n"))
    assert gh_token() == "gho_abc123"


def test_no_gh_is_not_an_error(command):
    command(CommandOutput(error="could not run gh: not found"))
    assert gh_token() == ""


def test_ssh_is_judged_on_githubs_greeting_not_the_exit_code(command):
    """GitHub answers a shell request with exit 1 and a greeting, so a
    non-zero status says nothing about whether the key works."""
    command(CommandOutput(
        error="ssh exited 1: Hi Morvin-Ian! You've successfully authenticated, "
              "but GitHub does not provide shell access."
    ))
    assert ssh_works() is True


def test_a_refused_key_is_not_authenticated(command):
    command(CommandOutput(error="ssh exited 255: Permission denied (publickey)."))
    assert ssh_works() is False


@pytest.mark.parametrize("transport,has_token,expected", [
    ("ssh", True, "git@github.com:o/n.git"),
    ("https", False, "https://x-access-token@github.com/o/n.git"),
    ("auto", True, "https://x-access-token@github.com/o/n.git"),
    ("auto", False, "git@github.com:o/n.git"),
])
def test_the_remote_follows_the_transport(transport, has_token, expected):
    assert remote_url("o/n", transport, has_token=has_token) == expected
