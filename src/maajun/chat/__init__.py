"""`maajun chat` — a conversational front end to maajun itself.

The agent knows every CLI command (discovered from the Typer tree, never a
hand-written list), can run the safe ones and propose the rest, and can
recall past incidents, pull requests, issues, and conversations from the
same database the daemon writes.
"""

from maajun.chat.memory import ChatMemory

__all__ = ["ChatMemory"]
