from __future__ import annotations

import asyncio

from maajun.agent.tools.base import Tool, json_schema, resolve_path
from maajun.providers.base import ToolDefinition


async def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    p = resolve_path(path)
    if not p.exists():
        return f"Error: {p} does not exist"
    if p.is_dir():
        return f"Error: {p} is a directory, not a file"
    try:
        text = await asyncio.to_thread(p.read_text, errors="replace")
    except Exception as e:
        return f"Error reading {p}: {e}"
    lines = text.splitlines()
    total = len(lines)
    if offset >= total > 0:
        return f"Error: offset {offset} is past the end of {p} ({total} lines)"
    window = lines[offset : offset + limit]
    numbered = [f"{i + offset + 1}: {line}" for i, line in enumerate(window)]
    header = f"File: {p} ({total} lines total, showing {offset + 1}-{min(offset + limit, total)})"
    return header + "\n" + "\n".join(numbered)


READ_FILE: Tool = Tool(
    ToolDefinition(
        name="read_file",
        description=(
            "Read a file's contents. Returns numbered lines. "
            "Use offset/limit for large files."
        ),
        parameters=json_schema(
            {
                "path": {"type": "string", "description": "Absolute file path"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start from (0-indexed, default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return (default 2000)",
                },
            },
            required=["path"],
        ),
    ),
    read_file,
)


async def edit_file(path: str, old_string: str, new_string: str) -> str:
    p = resolve_path(path)
    if not p.exists():
        return f"Error: {p} does not exist"
    try:
        text = await asyncio.to_thread(p.read_text, errors="replace")
    except Exception as e:
        return f"Error reading {p}: {e}"
    count = text.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {p}"
    if count > 1:
        return (
            f"Error: old_string found {count} times in {p}. "
            "Provide more surrounding context to make it unique."
        )
    await asyncio.to_thread(p.write_text, text.replace(old_string, new_string, 1))
    return f"Edited {p}"


EDIT_FILE: Tool = Tool(
    ToolDefinition(
        name="edit_file",
        description=(
            "Perform an exact string replacement in a file. "
            "The old_string must match exactly once. "
            "Use read_file first to see the file's contents."
        ),
        parameters=json_schema(
            {
                "path": {"type": "string", "description": "Absolute file path"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace (must appear exactly once)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            required=["path", "old_string", "new_string"],
        ),
    ),
    edit_file,
    requires_permission=True,
)


def write_file_sync(p, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


async def write_file(path: str, content: str) -> str:
    p = resolve_path(path)
    await asyncio.to_thread(write_file_sync, p, content)
    return f"Wrote {len(content)} bytes to {p}"


WRITE_FILE: Tool = Tool(
    ToolDefinition(
        name="write_file",
        description=(
            "Write content to a file. Creates parent directories "
            "if needed. Overwrites existing files."
        ),
        parameters=json_schema(
            {
                "path": {"type": "string", "description": "Absolute file path"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
            required=["path", "content"],
        ),
    ),
    write_file,
    requires_permission=True,
)
