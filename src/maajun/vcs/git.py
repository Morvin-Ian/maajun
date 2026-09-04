
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from maajun.agent.tools.sandbox import PRIVATE_NAMES, is_secret
from maajun.vcs.github import GitHubClient, GitHubError

ASKPASS_SCRIPT = '#!/bin/sh\necho "$MAAJUN_GIT_TOKEN"\n'

COMMIT_AUTHOR = "maajun"
COMMIT_EMAIL = "maajun@localhost"

# Long enough for a cold clone, short enough not to stall the poll loop.
GIT_TIMEOUT = 120
MAX_REVIEW_FILE_BYTES = 100_000


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
        self.commit_email: str | None = None

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
            await self.discard_local_changes()
        try:
            await self.git("checkout", "-B", base_branch, f"origin/{base_branch}")
        except GitError as err:
            raise GitError(
                f"Branch '{base_branch}' not found in {self.repo}. "
                "Check the branch name or use --base-branch."
            ) from err

    async def discard_local_changes(self) -> None:
        """Put the clone back to HEAD, so no earlier run's edits survive.

        One clone serves every incident in a repo. A run that died after the
        agent edited files — an unusable report, a git or GitHub failure, a
        killed daemon — leaves them on disk, and `checkout -B` carries them
        onto the next incident's branch, where `has_changes` reads them as
        that incident's fix. That is a pull request whose diff belongs to a
        different error.

        Ignored files are left alone (`clean -fd`, not `-fdx`): a virtualenv
        or a node_modules in the clone is expensive to rebuild and is not a
        change anyone is reviewing.
        """
        await self.git("reset", "--hard")
        await self.git("clean", "-fd")

    async def create_branch(self, branch: str, base_branch: str) -> None:
        await self.git("checkout", "-B", branch, f"origin/{base_branch}")

    async def committed_files(self, base_branch: str) -> list[str]:
        """The paths this branch's commits change, against the base branch.

        What the pull request's Files tab will show, asked before the push.
        """
        output = await self.git(
            "diff", "--name-only", f"origin/{base_branch}...HEAD"
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    async def has_changes(self) -> bool:
        return bool(await self.git("status", "--porcelain"))

    async def changed_files(self) -> list[str]:
        """Every path the working tree differs from HEAD in, staged or not."""
        lines = (await self.git("status", "--porcelain")).splitlines()
        paths = []
        for line in lines:
            path = line[3:].strip().strip('"')
            if not path:
                continue
            # "R  old -> new": the new name is the one on disk.
            paths.append(path.rpartition(" -> ")[2] or path)
        return paths

    async def working_diff(self) -> str:
        """The uncommitted patch for a bounded, read-only publication review."""
        tracked_names = await self.git(
            "diff", "HEAD", "--name-only", "-z", "--no-ext-diff"
        )
        reviewable_tracked = [
            relative
            for relative in tracked_names.split("\0")
            if relative and self.reviewable_path(relative)
        ]
        tracked = ""
        if reviewable_tracked:
            tracked = await self.git(
                "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--",
                *reviewable_tracked,
            )
        status = await self.git("status", "--porcelain", "-z", "--untracked-files=all")
        additions = []
        for entry in status.split("\0"):
            if not entry.startswith("?? "):
                continue
            relative = entry[3:]
            path = self.path / relative
            if not path.is_file() or not self.reviewable_path(relative):
                continue
            try:
                content = path.read_text(errors="replace")
            except OSError:
                continue
            additions.append(
                f"diff --git a/{relative} b/{relative}\n"
                f"new file mode 100644\n--- /dev/null\n+++ b/{relative}\n"
                + "\n".join(f"+{item}" for item in content.splitlines())
            )
        return "\n".join(part for part in (tracked, *additions) if part)

    def reviewable_path(self, relative: str) -> bool:
        """Whether a proposed file is safe and bounded enough for a prompt."""
        path = self.path / relative
        try:
            root = self.path.resolve()
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                return False
            if (
                is_secret(path)
                or is_secret(resolved)
                or path.name in PRIVATE_NAMES
                or resolved.name in PRIVATE_NAMES
                or ".git" in resolved.relative_to(root).parts
            ):
                return False
            return not path.exists() or resolved.stat().st_size <= MAX_REVIEW_FILE_BYTES
        except OSError:
            return False

    async def apply_patches(self, patches: list[str]) -> None:
        """Apply unified diffs to the working tree, all or none.

        One `git apply` over one stream, because that is what makes the
        all-or-none real: it refuses the whole batch and leaves the tree
        alone. Checking each patch separately does not — two fences against
        the same file both fit the pristine tree, and the second then fails
        on top of the first. That pair is a fix plus its regression test.
        """
        if not patches:
            return
        await asyncio.to_thread(self._apply, "".join(patches))

    def _apply(self, patch: str) -> None:
        args = ["apply", "--whitespace=nowarn", "-"]  # "-" reads it from stdin
        try:
            proc = subprocess.run(
                ["git", *args],
                input=patch,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                cwd=str(self.path),
                env=self.auth_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git apply timed out after {GIT_TIMEOUT}s") from e
        except OSError as e:
            raise GitError(f"could not run git apply: {e}") from e
        if proc.returncode != 0:
            raise GitError(f"git apply failed: {proc.stderr.strip()}")

    async def commit_all(self, message: str) -> None:
        commit_email = await self.resolve_commit_email()
        await self.git("add", "-A")
        await self.git(
            "-c", f"user.name={COMMIT_AUTHOR}",
            "-c", f"user.email={commit_email}",
            "commit", "-m", message,
        )

    async def resolve_commit_email(self) -> str:
        """Attribute generated commits to the GitHub account doing the push.

        The visible author remains maajun. GitHub maps its ID-based noreply
        address back to the authenticated user, which lets deployment tools
        verify that the commit author can access their project.
        """
        if self.commit_email:
            return self.commit_email
        if not self.token:
            return COMMIT_EMAIL

        client = GitHubClient(self.token)
        try:
            account = await client.authenticated_account()
        except GitHubError as error:
            raise GitError(f"could not identify GitHub commit author: {error}") from error
        finally:
            await client.aclose()
        self.commit_email = account.noreply_email
        return self.commit_email

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
