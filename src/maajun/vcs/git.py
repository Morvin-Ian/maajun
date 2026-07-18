"""Git workspace — an isolated clone the daemon works in.

Token auth uses GIT_ASKPASS so the token never lands in the remote URL,
.git/config, or the process list.
"""

from __future__ import annotations

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

    def _auth_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.token:
            askpass = self.root / "askpass.sh"
            if not askpass.exists():
                self.root.mkdir(parents=True, exist_ok=True)
                askpass.write_text(ASKPASS_SCRIPT)
                askpass.chmod(stat.S_IRWXU)
            env["GIT_ASKPASS"] = str(askpass)
            env["MAAJUN_GIT_TOKEN"] = self.token
            # Never fall back to an interactive prompt in the daemon.
            env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _git(self, *args: str, cwd: Path | None = None) -> str:
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

    def sync(self, base_branch: str) -> None:
        """Clone if missing, otherwise fetch; leaves base branch up to date."""
        if not (self.path / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self._git("clone", self.remote_url, str(self.path), cwd=self.root)
        else:
            self._git("fetch", "origin")
        self._git("checkout", "-B", base_branch, f"origin/{base_branch}")

    def create_branch(self, branch: str, base_branch: str) -> None:
        self._git("checkout", "-B", branch, f"origin/{base_branch}")

    def has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def commit_all(self, message: str) -> None:
        self._git("add", "-A")
        self._git(
            "-c", f"user.name={COMMIT_AUTHOR}",
            "-c", f"user.email={COMMIT_EMAIL}",
            "commit", "-m", message,
        )

    def push(self, branch: str) -> None:
        self._git("push", "--force-with-lease", "origin", f"{branch}:{branch}")

    def diff_stat(self) -> str:
        return self._git("diff", "--stat", "HEAD~1")
