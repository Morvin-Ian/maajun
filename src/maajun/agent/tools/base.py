"""Tool types and registry.

A tool is a 2-tuple of (ToolDefinition, executor coroutine). Definitions
are sent to the LLM; the agent loop calls executors.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from typing import Any

from maajun.providers.base import ToolDefinition

ToolExecutor = Callable[..., Coroutine[Any, Any, str]]
Tool = tuple[ToolDefinition, ToolExecutor]


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
        self._tools[tool[0].name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [definition for definition, _ in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        _, executor = tool
        try:
            return await executor(**arguments)
        except TypeError as e:
            return f"Error calling tool '{name}': {e}"
        except Exception as e:
            return f"Tool '{name}' failed: {type(e).__name__}: {e}"
