from __future__ import annotations

from collections.abc import Callable
from typing import Any

from maajun.agent.core import PermissionCallback
from maajun.chat.tools.commands import Gate, classify, parse_args

Confirm = Callable[[str], bool]


def describe(name: str, args: dict[str, Any]) -> str:
    """The one-line 'this is what I am about to do' shown before asking."""
    if name == "run_maajun_command":
        command = args.get("command", "")
        extra = str(args.get("args") or "").strip()
        return f"maajun {command} {extra}".rstrip()
    if name in ("edit_file", "write_file"):
        return f"{name} {args.get('path', '')}".rstrip()
    return f"{name} {args}"


def chat_permissions(confirm: Confirm) -> PermissionCallback:
    async def approve(name: str, args: dict[str, Any]) -> bool:
        if name == "run_maajun_command":
            command = str(args.get("command", ""))
            try:
                argv = parse_args(str(args.get("args") or ""))
            except ValueError:
                # Unparseable arguments: let the tool report the quoting error
                # rather than asking the user to approve something malformed.
                return True
            gate = classify(command, argv)
            if gate is Gate.BLOCKED:
                return False
            if gate is Gate.READ_ONLY:
                return True
        return confirm(describe(name, args))

    return approve
