"""The world the daemon tests run in: a fake agent, a fake GitHub, a real git.

Shared by `test_core` (the watching loop) and `test_investigation` (one
incident, from prompt to artifact). The fixtures that assemble them are in
`conftest.py`.
"""

import subprocess
from pathlib import Path

from maajun.providers.base import CompletionResponse
from maajun.vcs import GitHubError

TRACEBACK = """\
Traceback (most recent call last):
  File "/app/main.py", line 42, in handler
    result = items[index]
IndexError: list index out of range
"""


REPORT = """# IndexError in handler

## What happened
Requests to /items with an empty cart returned a 500. The handler indexed
the first element of a list that can be empty.

## Root cause
`main.py:12` reads `items[0]` without checking the list. Off-by-one on an
empty collection: the caller guarantees a list, never a non-empty one.

## Suggested fix
Guard the access and return an empty response instead.
"""


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


class FakeAgent:
    def __init__(self, report=REPORT, edit_path: Path | None = None):
        self.report = report
        self.edit_path = edit_path
        self.prompts: list[str] = []
        self.closed = False
        # Set to answer differently per round, e.g. an unusable first reply.
        self.replies: list[str] | None = None
        self.usage_per_call = {"prompt_tokens": 1000, "completion_tokens": 100}

    async def chat(self, message):
        self.prompts.append(message)
        if self.edit_path:
            # The clone only exists once the run has synced it, so a test can
            # aim at a path whose directory is not there yet.
            self.edit_path.parent.mkdir(parents=True, exist_ok=True)
            self.edit_path.write_text("items = [0]\n")
        content = self.replies.pop(0) if self.replies else self.report
        return CompletionResponse(content=content, usage=dict(self.usage_per_call))

    def take_usage(self):
        # The real agent hands over what it spent and forgets it; a fake
        # without this loses the cost of a failed run silently.
        return {}

    @property
    def model(self):
        return "fake-model"

    async def aclose(self):
        self.closed = True


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.closed = False
        self.visibilities = {}
        self.visibility_failures = {}
        self.visibility_calls = []

    async def repository_visibility(self, repo):
        self.visibility_calls.append(repo)
        if repo in self.visibility_failures:
            raise GitHubError(self.visibility_failures[repo])
        return self.visibilities.get(repo, "private")

    async def create_pull_request(self, repo, *, head, base, title, body):
        self.calls.append(
            {"repo": repo, "head": head, "base": base, "title": title, "body": body}
        )
        return f"https://github.com/{repo}/pull/{len(self.calls)}"

    async def create_issue(self, repo, *, title, body):
        self.issues.append({"repo": repo, "title": title, "body": body})
        return f"https://github.com/{repo}/issues/{len(self.issues)}"

    async def aclose(self):
        self.closed = True


def fix_mode(daemon, agent=None, *, test_command: str = "") -> None:
    """Switch to fix mode and let the agent actually change something.

    Both halves matter now: fix mode with no code change deliberately falls
    back to an issue, so a test about pull requests needs an agent that edits.
    """
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    repo_config.test_command = test_command
    if agent is not None:
        agent.edit_path = daemon.workspaces[repo_config.repo].path / "main.py"
