from __future__ import annotations

import re
import shlex
from pathlib import Path

from maajun.config import DeploymentConfig

EXEC_PATH_RE = re.compile(r"\bpath=([^\s;}]+)")


def command_executable(command: str) -> str:
    """Return the first absolute Python executable in a shell/systemd command."""
    match = EXEC_PATH_RE.search(command or "")
    if match:
        return match.group(1)
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return ""
    for token in tokens:
        if token.startswith("/") and Path(token).name in {
            "python",
            "python3",
            "pytest",
            "uvicorn",
            "gunicorn",
        }:
            return token
    return ""


def environment_root(executable: str) -> str:
    path = Path(executable)
    if path.parent.name != "bin":
        return ""
    return str(path.parent.parent)


def verification_runtime_mismatch(
    command: str, deployment: DeploymentConfig
) -> str:
    """Explain when a verification command uses a different Python runtime."""
    active = environment_root(command_executable(deployment.service_command))
    checking = environment_root(command_executable(command))
    if not active or not checking or Path(active) == Path(checking):
        return ""
    return (
        f"verification uses {checking}, but the active service uses {active}; "
        "treat failures as environment evidence, not as regressions from this fix"
    )
