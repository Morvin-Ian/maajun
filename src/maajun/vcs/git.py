
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

ASKPASS_SCRIPT = '#!/bin/sh\necho "$MAAJUN_GIT_TOKEN"\n'

COMMIT_AUTHOR = "maajun"
COMMIT_EMAIL = "maajun@localhost"

# Long enough for a cold clone, short enough not to stall the poll loop.
GIT_TIMEOUT = 120


class GitError(Exception):
    pass


@dataclass
class CommandResult:
    """Outcome of a workspace command, e.g. the configured test suite."""

    exit_code: int | None  # None when it timed out or could not start
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


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
        self.env: dict[str, str] | None = None

    def auth_env(self) -> dict[str, str]:
        """Build the git environment once and reuse it across commands.

        Writing the askpass helper and copying os.environ on every git call
        was pure overhead — the token and paths never change for a workspace.
        """
        if self.env is None:
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
            self.env = env
        return self.env

    def run(self, args: tuple[str, ...], cwd: Path | None) -> str:
        """Run one git command, or raise GitError.

        Timeouts and launch failures included: callers guard git with
        `except GitError`, and a bare TimeoutExpired sailed past all of them.
        """
        try:
            proc = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                cwd=str(cwd or self.path),
                env=self.auth_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(
                f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s"
            ) from e
        except OSError as e:
            raise GitError(f"could not run git {' '.join(args)}: {e}") from e
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    async def git(self, *args: str, cwd: Path | None = None) -> str:
        """Run a git command in a worker thread, off the event loop."""
        return await asyncio.to_thread(self.run, args, cwd)

    async def sync(self, base_branch: str) -> None:
        """Clone if missing, otherwise fetch; leaves base branch up to date."""
        if not (self.path / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            await self.git("clone", self.remote_url, str(self.path), cwd=self.root)
        else:
            await self.git("fetch", "origin")
        try:
            await self.git("checkout", "-B", base_branch, f"origin/{base_branch}")
        except GitError as err:
            raise GitError(
                f"Branch '{base_branch}' not found in {self.repo}. "
                "Check the branch name or use --base-branch."
            ) from err

    async def create_branch(self, branch: str, base_branch: str) -> None:
        await self.git("checkout", "-B", branch, f"origin/{base_branch}")

    async def has_changes(self) -> bool:
        return bool(await self.git("status", "--porcelain"))

    async def commit_all(self, message: str) -> None:
        await self.git("add", "-A")
        await self.git(
            "-c", f"user.name={COMMIT_AUTHOR}",
            "-c", f"user.email={COMMIT_EMAIL}",
            "commit", "-m", message,
        )

    async def push(self, branch: str) -> None:
        await self.git("push", "--force-with-lease", "origin", f"{branch}:{branch}")

    async def recent_commits(self, limit: int = 10) -> list[str]:
        """The newest commits on the checked-out branch, as "sha subject".

        Feeds deploy blame: an error that started after a specific commit is
        the single most useful thing to tell whoever is on call, and the clone
        is already on disk when the analysis runs.
        """
        try:
            output = await self.git(
                "log", f"-{limit}", "--no-merges", "--format=%h %s",
            )
        except GitError:
            # A shallow or freshly-initialized clone has no history to show.
            return []
        return [line for line in output.splitlines() if line.strip()]

    def run_shell(self, command: str, timeout: float) -> CommandResult:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.path),
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                exit_code=None,
                output=f"Timed out after {timeout:.0f}s.",
            )
        except OSError as e:
            return CommandResult(exit_code=None, output=f"Could not run: {e}")
        return CommandResult(
            exit_code=proc.returncode,
            output=f"{proc.stdout}{proc.stderr}".strip(),
        )

    async def run_command(self, command: str, *, timeout: float = 600) -> CommandResult:
        """Run a configured shell command in the workspace.

        Deliberately *not* an agent tool: the command comes from the user's
        config, never from the model, so verification can't be talked into
        running something else. Never raises — a failing or absent test
        command is a result to report, not a reason to abort the incident.
        """
        return await asyncio.to_thread(self.run_shell, command, timeout)
