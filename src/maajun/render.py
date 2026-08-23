"""Showing what the model wrote as markdown rather than as its source.

Every answer and every report is markdown, so a reader should see a heading
and a bullet, not `##` and `-`.

Streaming is the awkward part: text arrives a token at a time and markdown is
a block format — a fenced code block, a list, a table mean nothing until they
are finished. MarkdownStream buffers and releases a whole block at a time, so
the reader still watches the answer land in pieces.
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.markdown import Heading, Markdown

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
