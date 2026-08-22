from __future__ import annotations

import subprocess
from dataclasses import dataclass

COMMAND_TIMEOUT = 30.0


@dataclass
class CommandOutput:
    """What a command produced. `error` is empty when it worked."""

    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def text(self) -> str:
        return self.stdout.strip()


def run_text(cmd: list[str], *, timeout: float = COMMAND_TIMEOUT) -> CommandOutput:
    """Run a command and capture its output."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return CommandOutput(error=f"{cmd[0]} timed out after {timeout:.0f}s")
    except OSError as e:
        # FileNotFoundError included: the tool is not installed here.
        return CommandOutput(error=f"could not run {cmd[0]}: {e}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return CommandOutput(
            error=f"{cmd[0]} exited {result.returncode}: "
                  f"{detail[0] if detail else 'no output'}"
        )
    return CommandOutput(stdout=result.stdout, stderr=result.stderr)

