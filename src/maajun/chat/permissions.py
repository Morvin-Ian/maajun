from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from maajun.agent.core import PermissionCallback
from maajun.agent.tools.base import resolve_path
from maajun.chat.tools.commands import Gate, classify, parse_args

# Beyond yes and no: "always" stops asking, anything else is a reason the
# model is told about.
ALWAYS = "always"

Confirm = Callable[[str], bool | str]

# Enough to see the shape of a change; a rewrite is summarized instead.
DIFF_LINES = 24


def diff(old: str, new: str) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=2,
    ))[2:]
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + [f"… {len(lines) - DIFF_LINES} more diff lines"]
    return "\n".join(lines)


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def describe(name: str, args: dict[str, Any]) -> str:
    """What is about to happen, in enough detail to answer yes or no to.

    A write shows its diff: approving `edit_file config.toml` without seeing
    the change is not consent, it is a coin toss. Whether the path is one
    maajun may touch at all is the sandbox's question, asked before this one.
    """
    if name == "run_maajun_command":
        command = args.get("command", "")
        extra = str(args.get("args") or "").strip()
        return f"maajun {command} {extra}".rstrip()

    if name not in ("edit_file", "write_file"):
        return f"{name} {args}"

    path = resolve_path(str(args.get("path", "")))
    header = f"{name} {path}"

    if name == "edit_file":
        body = diff(str(args.get("old_string", "")), str(args.get("new_string", "")))
    else:
        content = str(args.get("content", ""))
        if path.exists():
            body = diff(read(path), content)
        else:
            body = f"new file, {len(content)} bytes"
    return f"{header}\n{body}" if body else header


def chat_permissions(confirm: Confirm) -> PermissionCallback:
    always: set[str] = set()

    async def approve(name: str, args: dict[str, Any]) -> bool | str:
        if name == "run_maajun_command":
            command = str(args.get("command", ""))
            try:
                argv = parse_args(str(args.get("args") or ""))
            except ValueError:
                # Let the tool report the quoting error rather than asking
                # the user to approve something malformed.
                return True
            gate = classify(command, argv)
            if gate is Gate.BLOCKED:
                return False
            if gate is Gate.READ_ONLY:
                return True
        if name in always:
            return True
        verdict = confirm(describe(name, args))
        if verdict == ALWAYS:
            always.add(name)
            return True
        return verdict

    return approve
