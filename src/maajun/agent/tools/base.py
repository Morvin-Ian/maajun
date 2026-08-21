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
    # Tools that modify files or run commands need user approval before
    # each call; the agent denies them when no approval handler is set.
    requires_permission: bool = False
    # Tools that open files they found themselves rather than files they were
    # handed. The registry passes them the sandbox as a `sandbox` keyword so
    # they can gate each one; it is never part of the schema the model sees.
    walks_files: bool = False


# Ceiling on a single tool result. read_file defaults to 2000 lines and grep
# to 50 matches, either of which can run to hundreds of kilobytes — and every
# result stays in the request for the rest of the tool loop. Truncating here
# rather than in each executor means a tool added later cannot forget to.
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

        Checked here rather than in each executor: every file tool names its
        target `path`, and one gate cannot be forgotten by the next tool
        somebody adds. The tool's schema decides whether it takes a path, not
        the arguments — grep and list_dir default to the working directory,
        which is a way out of the sandbox when the model simply omits it.

        This gates the path the call names. A tool that then walks that path
        and opens what it finds needs Sandbox.readable per file as well; see
        Tool.walks_files.
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
