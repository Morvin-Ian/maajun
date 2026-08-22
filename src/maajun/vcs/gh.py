from __future__ import annotations

import asyncio
import logging
import subprocess

from maajun.utils.commands import run_text

log = logging.getLogger(__name__)

SSH_REMOTE = "git@github.com:{repo}.git"
HTTPS_REMOTE = "https://x-access-token@github.com/{repo}.git"

TOKEN_URL = "https://github.com/settings/personal-access-tokens"
SSH_SETUP_URL = "https://docs.github.com/authentication/connecting-to-github-with-ssh"

INSTALL_GH = """\
Install the GitHub CLI and log in once — maajun then needs no token of its own:

  macOS         brew install gh
  Debian/Ubuntu sudo apt install gh
  Fedora        sudo dnf install gh
  other         https://cli.github.com

  gh auth login          # choose SSH if you push over SSH
"""


def gh_token() -> str:
    """The token from a `gh` login, or "" if gh is absent or logged out."""
    result = run_text(["gh", "auth", "token"], timeout=10)
    if result.error:
        log.debug("no gh token: %s", result.error)
        return ""
    return result.text()


def gh_account() -> str:
    """The login `gh` is authenticated as, or "" — for showing who is used."""
    result = run_text(
        ["gh", "api", "user", "--jq", ".login"], timeout=10
    )
    return "" if result.error else result.text()


def account_login(token: str | None = None) -> str:
    """The GitHub login maajun is authenticated as, or "".

    Asked of `gh` first, which is local and free; with a stored token it
    costs one API call. Used to complete a repo name given without an owner.
    """
    account = gh_account()
    if account:
        return account
    if not token:
        return ""
    from maajun.vcs.github import GitHubClient, GitHubError

    client = GitHubClient(token)

    async def ask() -> str:
        try:
            return await client.validate_token()
        except GitHubError:
            return ""
        finally:
            await client.aclose()

    return asyncio.run(ask())


def gh_available() -> bool:
    return not run_text(["gh", "--version"], timeout=10).error


def gh_login() -> int:
    """Run `gh auth login` attached to this terminal, for its own prompts.

    Not run_text: gh opens a browser flow and asks its own questions, so it
    needs the terminal rather than captured pipes.
    """
    return subprocess.call(["gh", "auth", "login"])


def ssh_works() -> bool:
    """Whether this machine can already push to GitHub over SSH.

    GitHub answers a shell request with exit 1 and a greeting, so the text is
    the signal, not the status. BatchMode keeps a missing key from prompting.
    """
    result = run_text([
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-T", "git@github.com",
    ], timeout=15)
    return "successfully authenticated" in (result.stdout + result.stderr + result.error)


def remote_url(repo: str, transport: str, *, has_token: bool) -> str:
    """The URL to clone and push `repo` with.

    "auto" prefers the token: HTTPS works on any host with no key set up,
    while SSH needs one. With no token, SSH is the only way to push at all.
    """
    if transport == "ssh":
        return SSH_REMOTE.format(repo=repo)
    if transport == "https":
        return HTTPS_REMOTE.format(repo=repo)
    return HTTPS_REMOTE.format(repo=repo) if has_token else SSH_REMOTE.format(repo=repo)
