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

    def takes_path(self, name: str) -> bool:
        tool = self.tools.get(name)
        if tool is None:
            return False
        return "path" in tool.definition.parameters.get("properties", {})

    def requires_path(self, name: str) -> bool:
        """Whether the tool's schema makes `path` mandatory.

        Separates the tools that *default* their path — grep, list_dir, glob,
        which mean the root when they say nothing — from the ones where a
        missing path is a broken call.
        """
        tool = self.tools.get(name)
        if tool is None:
            return False
        return "path" in tool.definition.parameters.get("required", [])

    def normalize(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Absolutize the path a call names, against the sandbox root.

        Done before the permission check so the policy and the tool judge the
        same path, and a relative one is not measured against the wrong root.
        An omitted path becomes the root for the tools that allow one: grep
        and list_dir otherwise default to the process directory, which is both
        useless to the model and a way out of the sandbox. A tool that
        *requires* a path keeps the omission instead. Substituting the root
        there turned a write_file that had lost its path into a call the
        permission policy approved — against a directory — so the correction
        written for exactly that mistake never ran and the model was handed an
        IsADirectoryError instead.
        """
        if self.sandbox is None or not self.takes_path(name):
            return arguments
        given = str(arguments.get("path") or "")
        if not given:
            if self.requires_path(name):
                return arguments
            return {**arguments, "path": str(self.sandbox.roots[0])}
        return {**arguments, "path": str(self.sandbox.resolve(given))}

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
        return self.sandbox.refusal(self.sandbox.resolve(str(arguments.get("path") or ".")))

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        arguments = self.normalize(name, arguments)
        # Reached by the ungated tools; a gated one is corrected by the policy
        # before it gets here.
        if self.requires_path(name) and not arguments.get("path"):
            where = self.sandbox.roots[0] if self.sandbox else "the workspace"
            return f"Error: {name} needs a path. Pass an absolute path under {where}."
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
