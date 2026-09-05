from __future__ import annotations

import json
import re
from abc import abstractmethod
from typing import Any

from maajun.monitors.base import ErrorEvent, Monitor
from maajun.monitors.defaults import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    DEFAULT_TRACEBACK_HEADERS,
    TRACEBACK_LOOKAHEAD_LINES,
)

SELF_DESCRIBING_HEADERS = ("panic:", "Exception in thread ")

SEVERE_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b")

# Cap on carried-over text for tracebacks split across polls.
MAX_PENDING = 64 * 1024


class LogStreamMonitor(Monitor):
    """Turns a stream of log text into one event per error.

    Subclasses supply the text — a file, a journal, a container's stdout —
    and inherit the reading of it: an ERROR line followed by a traceback is
    one event, and text that may still be streaming is carried to the next
    poll. Set json_level_field to match structured logs on their level.
    """

    def __init__(
        self,
        error_pattern: str = DEFAULT_ERROR_PATTERN,
        *,
        json_level_field: str = "",
        json_level_values: frozenset[str] = DEFAULT_JSON_LEVEL_VALUES,
        traceback_headers: tuple[str, ...] | list[str] | None = None,
        traceback_lookahead: int = TRACEBACK_LOOKAHEAD_LINES,
        burst_threshold: int = 1,
        burst_window_seconds: float = 60.0,
    ):
        super().__init__(
            burst_threshold=burst_threshold,
            burst_window_seconds=burst_window_seconds,
        )
        self.error_level_re = re.compile(error_pattern)
        self.json_level_field = json_level_field
        self.json_level_values = json_level_values
        self.traceback_header_re = re.compile(
            "|".join(
                re.escape(header)
                for header in (traceback_headers or DEFAULT_TRACEBACK_HEADERS)
            )
        )
        self.traceback_lookahead = max(1, traceback_lookahead)

        self.carryover_text = ""

    @abstractmethod
    async def read_stream(self) -> str:
        """Whatever the source has produced since the last poll."""

    async def flush(self) -> list[ErrorEvent]:
        """Emit carried-over text plus any incomplete burst, unconditionally."""
        events, _ = self.parse(self.carryover_text, flush=True)
        self.carryover_text = ""
        self.hold_for_burst(events)
        return self.drain_burst_buffer()

    async def poll(self) -> list[ErrorEvent]:
        text = await self.read_stream()
        if not text:
            if self.carryover_text:
                # Quiet for a whole interval, so it is not still streaming.
                events, _ = self.parse(self.carryover_text, flush=True)
                self.carryover_text = ""
                return self.apply_burst_threshold(events)
            return []

        events, self.carryover_text = self.parse(self.carryover_text + text)
        if len(self.carryover_text) > MAX_PENDING:
            self.carryover_text = self.carryover_text[-MAX_PENDING:]

        return self.apply_burst_threshold(events)

    def matches_error_level(self, line: str) -> bool:
        """Whether a line reports an error, by JSON level or by regex."""
        if self.json_level_field:
            try:
                record: dict[str, Any] = json.loads(line)
                level = str(record.get(self.json_level_field, "")).lower()
                if level in self.json_level_values:
                    return True
            except (json.JSONDecodeError, TypeError):
                pass  # not a JSON line; fall through to the regex
        return bool(self.error_level_re.search(line))

    def is_traceback_start(self, line: str) -> bool:
        return bool(self.traceback_header_re.search(line))

    def is_severe_level(self, line: str) -> bool:
        return bool(SEVERE_LEVEL_RE.search(line))

    def following_traceback_index(
        self, lines: list[str], start: int, *, lookahead: int
    ) -> int | None:
        """Index of a traceback header within `lookahead` lines of `start`.

        Returns None as soon as another error line is reached: that line
        deserves its own event, and the traceback belongs to it rather than to
        the earlier one. Everything scanned past becomes context for the
        merged event, so the scan must stay short and must not cross a line
        that carries its own meaning.
        """
        limit = min(len(lines), start + lookahead)
        for index in range(start, limit):
            if self.is_traceback_start(lines[index]):
                return index
            if self.matches_error_level(lines[index]):
                return None
        return None

    def parse(self, text: str, flush: bool = False) -> tuple[list[ErrorEvent], str]:
        """Extract events from complete lines; return (events, carry-over).

        With flush=True nothing is carried: held-back text is emitted even
        if it looks incomplete.
        """
        events: list[ErrorEvent] = []
        lines = text.split("\n")
        tail = lines.pop()  # "" if text ended with a newline
        if flush and tail:
            lines.append(tail)
            tail = ""

        i = 0
        while i < len(lines):
            line = lines[i]

            if self.is_traceback_start(line):
                block, end = self.collect_traceback(lines, i)
                if end is None and not flush:
                    return events, "\n".join(lines[i:] + [tail])
                events.append(self.traceback_event(block))
                i = end if end is not None else len(lines)
                continue

            if self.matches_error_level(line):
                next_index = i + 1
                severe = self.is_severe_level(line)
                if severe and next_index == len(lines) and not flush:
                    # A traceback may be right behind this line; wait one poll.
                    return events, "\n".join([line, tail])
                # A severe line's traceback often lands a line or two later;
                # anything else must follow immediately to count as the same.
                traceback_index = self.following_traceback_index(
                    lines,
                    next_index,
                    lookahead=self.traceback_lookahead if severe else 1,
                )
                if traceback_index is not None:
                    block, end = self.collect_traceback(lines, traceback_index)
                    if end is None and not flush:
                        return events, "\n".join(lines[i:] + [tail])
                    context = "\n".join(lines[i:traceback_index]).strip()
                    events.append(self.traceback_event(block, context=context))
                    i = end if end is not None else len(lines)
                    continue
                events.append(ErrorEvent(
                    source=self.name,
                    message=line.strip()[:200],
                    details=line.strip(),
                ))
            i += 1

        return events, tail

    def collect_traceback(self, lines: list[str], start: int) -> tuple[list[str], int | None]:
        """Collect a traceback starting at lines[start].

        Returns (block, index after block), or (partial, None) if the
        traceback runs to the end of the available lines (still streaming).
        """
        block = [lines[start]]
        i = start + 1
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                # A blank line belongs to a chained trace, but one followed
                # by an unindented line ends it — swallowing that retitled it.
                if self.blank_ends_block(lines, i):
                    return block, i + 1
                block.append(line)
                i += 1
                continue
            if line.startswith((" ", "\t")) or self.is_traceback_start(line):
                block.append(line)
                i += 1
                continue
            # A Go goroutine header is followed by an unindented function name.
            if block[-1].startswith("goroutine "):
                block.append(line)
                i += 1
                continue
            # The first other unindented line is the exception.
            block.append(line)
            return block, i + 1
        return block, None

    def blank_ends_block(self, lines: list[str], index: int) -> bool:
        """Whether the blank line at `index` is the end of the traceback.

        It is, unless the next non-blank line continues one: indented detail,
        or a chained-exception header like "During handling of the above".
        """
        for line in lines[index + 1:]:
            if not line.strip():
                continue
            return not (
                line.startswith((" ", "\t")) or self.is_traceback_start(line)
            )
        # Only blanks left; the caller decides whether more is coming.
        return False

    def traceback_event(self, block: list[str], context: str | None = None) -> ErrorEvent:
        details = "\n".join(block)
        if context:
            details = f"{context.strip()}\n{details}"
        return ErrorEvent(
            source=self.name,
            message=self.traceback_message(block)[:200],
            details=details,
        )

    @staticmethod
    def traceback_message(block: list[str]) -> str:
        """The one-line summary for a traceback block.

        Headers like "panic:" and "Exception in thread" self-describe the
        error — the exception IS the header. Separator-style headers
        (Traceback, Caused by) put the exception on the last non-indented,
        non-empty line instead.
        """
        header = block[0].strip()
        if header.startswith(SELF_DESCRIBING_HEADERS[0]) or SELF_DESCRIBING_HEADERS[1] in header:
            return header
        for line in reversed(block):
            if line.strip() and not line.startswith((" ", "\t")):
                stripped = line.strip()
                if stripped != header:
                    return stripped
        return header
