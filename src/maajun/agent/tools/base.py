"""Tool types and registry.

A tool is a 2-tuple of (ToolDefinition, executor coroutine). Definitions
are sent to the LLM; the agent loop calls executors.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any, NamedTuple

from maajun.agent.tools.sandbox import Sandbox
from maajun.providers.base import ToolDefinition


def resolve_path(path: str) -> Path:
    """Expand ~ and resolve to an absolute path."""
    return Path(path).expanduser().resolve()


ToolExecutor = Callable[..., Coroutine[Any, Any, str]]


class Tool(NamedTuple):
    definition: ToolDefinition
    executor: ToolExecutor
    # Denied outright when no approval handler is set.
    requires_permission: bool = False
    # Opens files it finds rather than files it is handed, so the registry
    # passes it the sandbox to gate each one. Not in the model's schema.
    walks_files: bool = False


# Every result stays in the request for the rest of the tool loop. Capped
# here rather than per-executor so a new tool cannot forget to.
MAX_TOOL_RESULT_CHARS = 30_000


def cap_result(result: str) -> str:
    """Truncate an oversized tool result, saying how much was dropped.

    The model needs to know it saw a partial answer — a silent cut invites it
    to conclude that the rest of the file simply does not exist.
    """
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    dropped = len(result) - MAX_TOOL_RESULT_CHARS
    return (
        result[:MAX_TOOL_RESULT_CHARS]
        + f"\n… [truncated: {dropped:,} more characters. Narrow the search, "
        "or re-read with offset/limit.]"
    )


def json_schema(props: dict, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = (), sandbox: Sandbox | None = None):
        self.tools: dict[str, Tool] = {}
        self.sandbox = sandbox
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self.tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self.tools.values()]

    def requires_permission(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and tool.requires_permission)

    def off_limits(self, tool: Tool, arguments: dict[str, Any]) -> str:
        """Why the sandbox refuses this call, or "" if it does not.

        Checked here, not per-executor, so the next tool cannot forget it. The
        *schema* decides whether a tool takes a path: grep and list_dir default
        to the cwd, which is a way out when the model omits it.

        This gates the path the call names; a tool that then walks it needs
        Sandbox.readable per file too. See Tool.walks_files.
        """
        if self.sandbox is None:
            return ""
        properties = tool.definition.parameters.get("properties", {})
        if "path" not in properties:
            return ""
        return self.sandbox.refusal(resolve_path(str(arguments.get("path") or ".")))

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        refusal = self.off_limits(tool, arguments)
        if refusal:
            return f"Error: {refusal}"
        if tool.walks_files:
            arguments = {**arguments, "sandbox": self.sandbox}
        try:
            return cap_result(await tool.executor(**arguments))
        except TypeError as e:
            return f"Error calling tool '{name}': {e}"
        except Exception as e:
            return f"Tool '{name}' failed: {type(e).__name__}: {e}"
