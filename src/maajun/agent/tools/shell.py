"""Shell tool: bash."""

from __future__ import annotations

import subprocess

from maajun.agent.tools.base import Tool, json_schema
from maajun.providers.base import ToolDefinition

MAX_OUTPUT = 10_000


async def _bash(command: str, timeout: int = 30) -> str:
    if not command.strip():
        return "Error: empty command"

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"

    output = proc.stdout
    if proc.stderr:
        output += ("\n" if output else "") + proc.stderr

    if len(output) > MAX_OUTPUT:
        truncated = len(output) - MAX_OUTPUT
        output = output[:MAX_OUTPUT] + f"\n... (truncated, {truncated} bytes remaining)"

    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"

    return output if output else "(no output)"


BASH: Tool = Tool(
    ToolDefinition(
        name="bash",
        description=(
            "Execute a shell command and return stdout/stderr. "
            "Commands run in the current working directory. "
            "Output is truncated at 10KB. Timeout after 30s by default."
        ),
        parameters=json_schema(
            {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
            },
            required=["command"],
        ),
    ),
    _bash,
    requires_permission=True,
)
