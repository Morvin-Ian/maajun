"""Tool types and registry.

A tool is a 2-tuple of (ToolDefinition, executor coroutine). Definitions
are sent to the LLM; the agent loop calls executors.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any, NamedTuple

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


def json_schema(props: dict, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def requires_permission(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.requires_permission)

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            return await tool.executor(**arguments)
        except TypeError as e:
            return f"Error calling tool '{name}': {e}"
        except Exception as e:
            return f"Tool '{name}' failed: {type(e).__name__}: {e}"
