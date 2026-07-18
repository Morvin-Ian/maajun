"""Search tools: glob, grep, list_dir."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from maajun.agent.tools.base import Tool, json_schema
from maajun.providers.base import ToolDefinition

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt",
}


async def _glob(pattern: str, path: str = ".") -> str:
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return f"Error: {path} does not exist"
    results = [
        str(match.relative_to(root)) + ("/" if match.is_dir() else "")
        for match in sorted(root.glob(pattern))
    ]
    if not results:
        return f"No files matched pattern: {pattern}"
    return "\n".join(results)


GLOB: Tool = Tool(
    ToolDefinition(
        name="glob",
        description=(
            "Find files by glob pattern. Returns matching paths relative to the search root. "
            "Use ** for recursive matching (e.g. src/**/*.py)."
        ),
        parameters=json_schema(
            {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. **/*.py, src/**/*.ts)",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                },
            },
            required=["pattern"],
        ),
    ),
    _glob,
)


async def _grep(
    pattern: str,
    path: str = ".",
    include: str | None = None,
    max_results: int = 50,
) -> str:
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return f"Error: {path} does not exist"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    results: list[str] = []
    files_searched = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if include and not fnmatch.fnmatch(fname, include):
                continue
            fpath = Path(dirpath) / fname
            try:
                text = fpath.read_text(errors="replace")
            except Exception:
                continue
            files_searched += 1
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(root)
                    results.append(f"{rel}:{i}: {line.strip()}")
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    if not results:
        return f"No matches for /{pattern}/ (searched {files_searched} files)"
    header = f"Matches for /{pattern}/ ({len(results)} results, {files_searched} files searched):"
    return header + "\n" + "\n".join(results)


GREP: Tool = Tool(
    ToolDefinition(
        name="grep",
        description=(
            "Search file contents using regex. Returns file:line: content matches. "
            "Skips .git, node_modules, __pycache__, .venv, etc."
        ),
        parameters=json_schema(
            {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                },
                "include": {
                    "type": "string",
                    "description": "File glob to filter (e.g. *.py, *.ts)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 50)",
                },
            },
            required=["pattern"],
        ),
    ),
    _grep,
)


async def _list_dir(path: str = ".") -> str:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"Error: {p} does not exist"
    if not p.is_dir():
        return f"Error: {p} is not a directory"

    entries = [
        entry.name + ("/" if entry.is_dir() else "")
        for entry in sorted(p.iterdir())
    ]
    if not entries:
        return f"Directory {p} is empty"
    return "\n".join(entries)


LIST_DIR: Tool = Tool(
    ToolDefinition(
        name="list_dir",
        description="List the contents of a directory. Appends / to directory names.",
        parameters=json_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Directory path (default: current directory)",
                },
            },
        ),
    ),
    _list_dir,
)
