"""Git workspace — an isolated clone the daemon works in.

Token auth uses GIT_ASKPASS so the token never lands in the remote URL,
.git/config, or the process list.

Git commands shell out and can take seconds (clone, fetch, push), so each
runs in a worker thread — the async callers never block the event loop.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path

ASKPASS_SCRIPT = '#!/bin/sh\necho "$MAAJUN_GIT_TOKEN"\n'

COMMIT_AUTHOR = "maajun"
COMMIT_EMAIL = "maajun@localhost"


class GitError(Exception):
    pass


class GitWorkspace:
    def __init__(self, root: str | Path, repo: str, token: str | None = None,
                 remote_url: str | None = None):
        """
        root: directory that holds the clone (created if missing)
        repo: "owner/name" on GitHub
        remote_url: override for tests / non-GitHub remotes
        """
        self.root = Path(root).expanduser()
        self.repo = repo
        self.token = token
        self.remote_url = remote_url or f"https://x-access-token@github.com/{repo}.git"
        self.path = self.root / repo.replace("/", "__")
        self._env: dict[str, str] | None = None

    def _auth_env(self) -> dict[str, str]:
        """Build the git environment once and reuse it across commands.

        Writing the askpass helper and copying os.environ on every git call
        was pure overhead — the token and paths never change for a workspace.
        """
        if self._env is None:
            env = os.environ.copy()
            if self.token:
                askpass = self.root / "askpass.sh"
                self.root.mkdir(parents=True, exist_ok=True)
                if not askpass.exists():
                    askpass.write_text(ASKPASS_SCRIPT)
                    askpass.chmod(stat.S_IRWXU)
                env["GIT_ASKPASS"] = str(askpass)
                env["MAAJUN_GIT_TOKEN"] = self.token
                # Never fall back to an interactive prompt in the daemon.
                env["GIT_TERMINAL_PROMPT"] = "0"
            self._env = env
        return self._env

    def _run(self, args: tuple[str, ...], cwd: Path | None) -> str:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(cwd or self.path),
            env=self._auth_env(),
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    async def _git(self, *args: str, cwd: Path | None = None) -> str:
        """Run a git command in a worker thread, off the event loop."""
        return await asyncio.to_thread(self._run, args, cwd)

    async def sync(self, base_branch: str) -> None:
        """Clone if missing, otherwise fetch; leaves base branch up to date."""
        if not (self.path / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            await self._git("clone", self.remote_url, str(self.path), cwd=self.root)
        else:
            await self._git("fetch", "origin")
        try:
            await self._git("checkout", "-B", base_branch, f"origin/{base_branch}")
        except GitError as err:
            raise GitError(
                f"Branch '{base_branch}' not found in {self.repo}. "
                "Check the branch name or use --base-branch."
            ) from err

    async def create_branch(self, branch: str, base_branch: str) -> None:
        await self._git("checkout", "-B", branch, f"origin/{base_branch}")

    async def has_changes(self) -> bool:
        return bool(await self._git("status", "--porcelain"))

    async def commit_all(self, message: str) -> None:
        await self._git("add", "-A")
        await self._git(
            "-c", f"user.name={COMMIT_AUTHOR}",
            "-c", f"user.email={COMMIT_EMAIL}",
            "commit", "-m", message,
        )

    async def push(self, branch: str) -> None:
        await self._git("push", "--force-with-lease", "origin", f"{branch}:{branch}")

    async def diff_stat(self) -> str:
        return await self._git("diff", "--stat", "HEAD~1")
