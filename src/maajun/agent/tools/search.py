"""Search tools: glob, grep, list_dir."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path

from maajun.agent.tools.base import Tool, json_schema, resolve_path
from maajun.agent.tools.sandbox import Sandbox
from maajun.providers.base import ToolDefinition

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt",
}

# grep skips files bigger than this — a lockfile or a bundled asset is
# rarely what a code search wants, and reading it wastes time and memory.
MAX_FILE_SIZE = 5 * 1024 * 1024


def in_skip_dir(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


def glob_sync(root: Path, pattern: str) -> list[str]:
    results = []
    for match in sorted(root.glob(pattern)):
        rel = match.relative_to(root)
        if in_skip_dir(rel):
            continue
        results.append(str(rel) + ("/" if match.is_dir() else ""))
    return results


async def glob(pattern: str, path: str = ".") -> str:
    root = resolve_path(path)
    if not root.exists():
        return f"Error: {path} does not exist"
    # The root is checked before the call; '..' in the pattern would walk out
    # of it afterwards.
    if ".." in Path(pattern).parts:
        return "Error: '..' is not allowed in a glob pattern. Search from a path instead."
    results = await asyncio.to_thread(glob_sync, root, pattern)
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
    glob,
)


def is_probably_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def grep_sync(
    root: Path,
    regex: re.Pattern[str],
    include: str | None,
    max_results: int,
    sandbox: Sandbox | None,
) -> tuple[list[str], int, int]:
    """Search under `root`. Returns (matches, files searched, files refused).

    Every file is put to the sandbox before it is opened. The registry gates
    the directory this was pointed at, but grep then reads whatever is under
    it — so a .env or an id_rsa inside an allowed root would otherwise come
    straight back as matched lines.
    """
    results: list[str] = []
    files_searched = 0
    files_refused = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if include and not fnmatch.fnmatch(fname, include):
                continue
            fpath = Path(dirpath) / fname
            if sandbox is not None and not sandbox.readable(fpath):
                files_refused += 1
                continue
            try:
                if fpath.stat().st_size > MAX_FILE_SIZE:
                    continue
                data = fpath.read_bytes()
            except OSError:
                continue
            if is_probably_binary(data):
                continue
            files_searched += 1
            for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(root)
                    results.append(f"{rel}:{i}: {line.strip()}")
                    if len(results) >= max_results:
                        return results, files_searched, files_refused
    return results, files_searched, files_refused


async def grep(
    pattern: str,
    path: str = ".",
    include: str | None = None,
    max_results: int = 50,
    sandbox: Sandbox | None = None,
) -> str:
    root = resolve_path(path)
    if not root.exists():
        return f"Error: {path} does not exist"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    results, files_searched, files_refused = await asyncio.to_thread(
        grep_sync, root, regex, include, max_results, sandbox
    )

    # Said out loud so the model knows the search was not exhaustive, and
    # does not go looking for another way into the files it skipped.
    skipped = (
        f", {files_refused} off-limits files skipped"
        if files_refused
        else ""
    )
    if not results:
        return f"No matches for /{pattern}/ (searched {files_searched} files{skipped})"
    header = (
        f"Matches for /{pattern}/ ({len(results)} results, "
        f"{files_searched} files searched{skipped}):"
    )
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
    grep,
    walks_files=True,
)


def list_dir_sync(p: Path) -> list[str]:
    return [
        entry.name + ("/" if entry.is_dir() else "")
        for entry in sorted(p.iterdir())
    ]


async def list_dir(path: str = ".") -> str:
    p = resolve_path(path)
    if not p.exists():
        return f"Error: {p} does not exist"
    if not p.is_dir():
        return f"Error: {p} is not a directory"

    entries = await asyncio.to_thread(list_dir_sync, p)
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
    list_dir,
)
