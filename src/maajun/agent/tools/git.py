"""Git tools: git_status."""

from __future__ import annotations

import subprocess
from pathlib import Path

from maajun.agent.tools.base import Tool, json_schema
from maajun.providers.base import ToolDefinition


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd),
        )
        return r.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


async def _git_status(path: str = ".") -> str:
    p = Path(path).expanduser().resolve()

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], p)
    if branch.startswith("Error") or not branch:
        return f"Not a git repository or git error: {branch}"

    status = _run_git(["status", "--short"], p)
    log = _run_git(["log", "--oneline", "-10"], p)
    parts = [f"Branch: {branch}"]
    if status:
        parts.append(f"Status:\n{status}")
    if log:
        parts.append(f"Recent commits:\n{log}")
    return "\n\n".join(parts)


GIT_STATUS: Tool = (
    ToolDefinition(
        name="git_status",
        description="Show git branch, status, and recent commits for the repo at the given path.",
        parameters=json_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Repository root path (default: current directory)",
                },
            },
        ),
    ),
    _git_status,
)
