"""Search tools: glob, grep, list_dir."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path

from maajun.agent.tools.base import Tool, json_schema, resolve_path
from maajun.providers.base import ToolDefinition

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt",
}

# grep skips files bigger than this — a lockfile or a bundled asset is
# rarely what a code search wants, and reading it wastes time and memory.
MAX_FILE_SIZE = 5 * 1024 * 1024


def _in_skip_dir(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


def _glob_sync(root: Path, pattern: str) -> list[str]:
    results = []
    for match in sorted(root.glob(pattern)):
        rel = match.relative_to(root)
        if _in_skip_dir(rel):
            continue
        results.append(str(rel) + ("/" if match.is_dir() else ""))
    return results


async def _glob(pattern: str, path: str = ".") -> str:
    root = resolve_path(path)
    if not root.exists():
        return f"Error: {path} does not exist"
    results = await asyncio.to_thread(_glob_sync, root, pattern)
    if not results:
        return f"No files matched pattern: {pattern}"
    return "\n".join(results)


GLOB: Tool = Tool(
    ToolDefinition(
        name="glob",
        description=(
            "Find files by glob pattern. Returns matching paths relative to the search root. "
            "Use ** for recursive matching (e.g. src/**/*.py). "
            "Skips .git, node_modules, __pycache__, .venv, etc."
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


def _is_probably_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _grep_sync(
    root: Path, regex: re.Pattern[str], include: str | None, max_results: int
) -> tuple[list[str], int]:
    results: list[str] = []
    files_searched = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if include and not fnmatch.fnmatch(fname, include):
                continue
            fpath = Path(dirpath) / fname
            try:
                if fpath.stat().st_size > MAX_FILE_SIZE:
                    continue
                data = fpath.read_bytes()
            except OSError:
                continue
            if _is_probably_binary(data):
                continue
            files_searched += 1
            for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(root)
                    results.append(f"{rel}:{i}: {line.strip()}")
                    if len(results) >= max_results:
                        return results, files_searched
    return results, files_searched


async def _grep(
    pattern: str,
    path: str = ".",
    include: str | None = None,
    max_results: int = 50,
) -> str:
    root = resolve_path(path)
    if not root.exists():
        return f"Error: {path} does not exist"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    results, files_searched = await asyncio.to_thread(
        _grep_sync, root, regex, include, max_results
    )

    if not results:
        return f"No matches for /{pattern}/ (searched {files_searched} files)"
    header = f"Matches for /{pattern}/ ({len(results)} results, {files_searched} files searched):"
    return header + "\n" + "\n".join(results)


GREP: Tool = Tool(
    ToolDefinition(
        name="grep",
        description=(
            "Search file contents using regex. Returns file:line: content matches. "
            "Skips .git, node_modules, __pycache__, .venv, binaries, and large files."
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


def _list_dir_sync(p: Path) -> list[str]:
    return [
        entry.name + ("/" if entry.is_dir() else "")
        for entry in sorted(p.iterdir())
    ]


async def _list_dir(path: str = ".") -> str:
    p = resolve_path(path)
    if not p.exists():
        return f"Error: {p} does not exist"
    if not p.is_dir():
        return f"Error: {p} is not a directory"

    entries = await asyncio.to_thread(_list_dir_sync, p)
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
