from maajun.agent.tools.base import (
    MAX_TOOL_RESULT_CHARS,
    Tool,
    ToolExecutor,
    ToolRegistry,
    cap_result,
    json_schema,
    resolve_path,
)
from maajun.agent.tools.files import EDIT_FILE, READ_FILE, WRITE_FILE
from maajun.agent.tools.git_status import GIT_STATUS
from maajun.agent.tools.sandbox import Sandbox
from maajun.agent.tools.search import GLOB, GREP, LIST_DIR

BUILTIN_TOOLS: list[Tool] = [
    READ_FILE,
    EDIT_FILE,
    WRITE_FILE,
    GLOB,
    GREP,
    LIST_DIR,
    GIT_STATUS,
]


def default_registry(sandbox: Sandbox | None = None) -> ToolRegistry:
    return ToolRegistry(BUILTIN_TOOLS, sandbox)


__all__ = [
    "BUILTIN_TOOLS",
    "EDIT_FILE",
    "GIT_STATUS",
    "GLOB",
    "GREP",
    "LIST_DIR",
    "MAX_TOOL_RESULT_CHARS",
    "READ_FILE",
    "WRITE_FILE",
    "Sandbox",
    "Tool",
    "ToolExecutor",
    "ToolRegistry",
    "cap_result",
    "default_registry",
    "json_schema",
    "resolve_path",
]
