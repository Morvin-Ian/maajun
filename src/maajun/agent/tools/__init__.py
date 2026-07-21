from maajun.agent.tools.base import Tool, ToolExecutor, ToolRegistry, json_schema, resolve_path
from maajun.agent.tools.files import EDIT_FILE, READ_FILE, WRITE_FILE
from maajun.agent.tools.git import GIT_STATUS
from maajun.agent.tools.search import GLOB, GREP, LIST_DIR
from maajun.agent.tools.shell import BASH

BUILTIN_TOOLS: list[Tool] = [
    READ_FILE,
    EDIT_FILE,
    WRITE_FILE,
    GLOB,
    GREP,
    BASH,
    LIST_DIR,
    GIT_STATUS,
]


def default_registry() -> ToolRegistry:
    return ToolRegistry(BUILTIN_TOOLS)


__all__ = [
    "BASH",
    "BUILTIN_TOOLS",
    "EDIT_FILE",
    "GIT_STATUS",
    "GLOB",
    "GREP",
    "LIST_DIR",
    "READ_FILE",
    "WRITE_FILE",
    "Tool",
    "ToolExecutor",
    "ToolRegistry",
    "default_registry",
    "json_schema",
    "resolve_path",
]
