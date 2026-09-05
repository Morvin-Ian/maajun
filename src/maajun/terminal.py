from __future__ import annotations

import re
import time
from collections.abc import Generator
from contextlib import contextmanager

from rich.console import Console
from rich.live import Live
from rich.markdown import Heading, Markdown
from rich.spinner import Spinner
from rich.text import Text

# ``` or ~~~, indented up to three spaces, opening or closing a code block.
FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")


class LeftHeading(Heading):
    """Rich centres an h1. In a reply that reads as a banner, not an answer."""

    LEVEL_ALIGN = {**Heading.LEVEL_ALIGN, "h1": "left"}


class ReplyMarkdown(Markdown):
    elements = {**Markdown.elements, "heading_open": LeftHeading}


class MarkdownStream:
    """Buffers streamed text and hands back whole markdown blocks.

    A block ends at a blank line, except inside a fence, where the fence has
    to close first. Only lines that have arrived complete are considered:
    whatever follows the last newline is still being written.
    """

    def __init__(self) -> None:
        self.buffer = ""

    def write(self, chunk: str) -> list[str]:
        """Add a chunk, returning any blocks it completed."""
        self.buffer += chunk
        blocks = []
        while True:
            block, rest = self.take_block(self.buffer)
            if block is None:
                return blocks
            self.buffer = rest
            if block.strip():
                blocks.append(block)

    def close(self) -> list[str]:
        """Release the unterminated tail — the last block has no blank line."""
        remainder, self.buffer = self.buffer, ""
        return [remainder] if remainder.strip() else []

    @staticmethod
    def take_block(text: str) -> tuple[str | None, str]:
        last_newline = text.rfind("\n")
        if last_newline == -1:
            return None, text
        fenced = False
        consumed = 0
        for line in text[: last_newline + 1].splitlines(keepends=True):
            consumed += len(line)
            if FENCE_RE.match(line):
                fenced = not fenced
                # A closing fence ends the block: what follows is prose.
                if not fenced:
                    return text[:consumed], text[consumed:]
                continue
            if not fenced and not line.strip():
                return text[:consumed], text[consumed:]
        return None, text


def render(console: Console, block: str) -> None:
    """Print one markdown block.

    Markdown parses its own source, so text that happens to look like Rich
    markup — a `[note]` in a sentence, an argv snippet — is shown, not
    swallowed as a style tag.
    """
    console.print(ReplyMarkdown(block.strip(), code_theme="ansi_dark"))


class WorkingStatus:
    """Animated 'Analyzing with AI… (12s)' line for a multi-phase task.

    The phase label is mutable via `set`; the elapsed timer resets on each
    phase change so every step shows its own duration.
    """

    def __init__(self, phase: str):
        self.phase = phase
        self.started = time.monotonic()
        self.spinner = Spinner("dots", style="cyan")
        self.live: Live | None = None

    def set(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.started = time.monotonic()

    @contextmanager
    def paused(self) -> Generator[None, None, None]:
        """Take the spinner off the screen while something else reads input. """
        if self.live is None:
            yield
            return
        self.live.stop()
        try:
            yield
        finally:
            self.live.start()

    def __rich_console__(self, console, options):
        elapsed = int(time.monotonic() - self.started)
        self.spinner.update(text=Text(f"{self.phase}… ({elapsed}s)", style="cyan"))
        yield self.spinner


@contextmanager
def working(console: Console, phase: str) -> Generator[WorkingStatus, None, None]:
    """Show a transient 'working' spinner until the block exits. """
    status = WorkingStatus(phase)
    with Live(status, console=console, refresh_per_second=8, transient=True) as live:
        status.live = live
        yield status
