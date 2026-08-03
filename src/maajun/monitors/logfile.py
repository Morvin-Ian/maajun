"""Tail a log file and emit ErrorEvents for tracebacks and error lines."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from maajun.monitors.base import ErrorEvent, Monitor
from maajun.monitors.defaults import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    DEFAULT_TRACEBACK_HEADERS,
    TRACEBACK_LOOKAHEAD_LINES,
)

# Headers that *are* the exception, rather than introducing one on a later line.
_SELF_DESCRIBING_HEADERS = ("panic:", "Exception in thread ")

# Levels severe enough that a traceback may be logged a line or two behind them
# (the logging.exception pattern), so it's worth looking ahead to merge them.
_SEVERE_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b")

# Cap on carried-over text for tracebacks split across polls.
MAX_PENDING = 64 * 1024


class LogFileMonitor(Monitor):
    """Incrementally reads a log file, surviving rotation and truncation.

    Emits one event per error: an ERROR line immediately followed by a
    traceback (the logging.exception pattern) is merged into a single
    event rather than two. Text that might still be streaming in (an
    unterminated traceback, or an ERROR line that may have a traceback
    right behind it) is carried over and flushed on the next quiet poll.

    Detection is regex-based by default; set json_level_field to also match
    structured (one-JSON-object-per-line) logs on their level field.
    """

    def __init__(
        self,
        path: str | Path,
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
        self.path = Path(path).expanduser()
        self._error_level_re = re.compile(error_pattern)
        self._json_level_field = json_level_field
        self._json_level_values = json_level_values
        self._traceback_header_re = re.compile(
            "|".join(
                re.escape(header)
                for header in (traceback_headers or DEFAULT_TRACEBACK_HEADERS)
            )
        )
        self._traceback_lookahead = max(1, traceback_lookahead)

        self._offset = 0
        self._inode: int | None = None
        self._carryover_text = ""

    # -- Monitor interface ----------------------------------------------

    @property
    def name(self) -> str:
        return f"logfile:{self.path}"

    async def flush(self) -> list[ErrorEvent]:
        """Emit carried-over text plus any incomplete burst, unconditionally."""
        events, _ = self._parse(self._carryover_text, flush=True)
        self._carryover_text = ""
        self._hold_for_burst(events)
        return self._drain_burst_buffer()

    async def poll(self) -> list[ErrorEvent]:
        text = self._read_new()
        if not text:
            if self._carryover_text:
                # Nothing new for a whole interval — what we held back is
                # not still streaming; emit it.
                events, _ = self._parse(self._carryover_text, flush=True)
                self._carryover_text = ""
                return self._apply_burst_threshold(events)
            return []

        events, self._carryover_text = self._parse(self._carryover_text + text)
        if len(self._carryover_text) > MAX_PENDING:
            self._carryover_text = self._carryover_text[-MAX_PENDING:]

        return self._apply_burst_threshold(events)

    # -- file I/O -------------------------------------------------------

    def _read_new(self) -> str:
        if not self.path.exists():
            return ""
        stat = self.path.stat()
        rotated = self._inode is not None and stat.st_ino != self._inode
        truncated = stat.st_size < self._offset
        if rotated or truncated:
            self._offset = 0
            self._carryover_text = ""
        self._inode = stat.st_ino

        if stat.st_size == self._offset:
            return ""
        with open(self.path, errors="replace") as f:
            f.seek(self._offset)
            text = f.read()
            self._offset = f.tell()
        return text

    # -- parsing --------------------------------------------------------

    def _matches_error_level(self, line: str) -> bool:
        """Whether a line reports an error, by JSON level or by regex."""
        if self._json_level_field:
            try:
                record: dict[str, Any] = json.loads(line)
                level = str(record.get(self._json_level_field, "")).lower()
                if level in self._json_level_values:
                    return True
            except (json.JSONDecodeError, TypeError):
                pass  # not a JSON line; fall through to the regex
        return bool(self._error_level_re.search(line))

    def _is_traceback_start(self, line: str) -> bool:
        return bool(self._traceback_header_re.search(line))

    def _is_severe_level(self, line: str) -> bool:
        return bool(_SEVERE_LEVEL_RE.search(line))

    def _following_traceback_index(
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
            if self._is_traceback_start(lines[index]):
                return index
            if self._matches_error_level(lines[index]):
                return None
        return None

    def _parse(self, text: str, flush: bool = False) -> tuple[list[ErrorEvent], str]:
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

            if self._is_traceback_start(line):
                block, end = self._collect_traceback(lines, i)
                if end is None and not flush:
                    return events, "\n".join(lines[i:] + [tail])
                events.append(self._traceback_event(block))
                i = end if end is not None else len(lines)
                continue

            if self._matches_error_level(line):
                next_index = i + 1
                severe = self._is_severe_level(line)
                if severe and next_index == len(lines) and not flush:
                    # A traceback may be right behind this line; wait one poll.
                    return events, "\n".join([line, tail])
                # Severe lines get a short lookahead (the traceback often lands
                # a line or two later); anything else must be followed
                # immediately to count as the same failure.
                traceback_index = self._following_traceback_index(
                    lines,
                    next_index,
                    lookahead=self._traceback_lookahead if severe else 1,
                )
                if traceback_index is not None:
                    block, end = self._collect_traceback(lines, traceback_index)
                    if end is None and not flush:
                        return events, "\n".join(lines[i:] + [tail])
                    context = "\n".join(lines[i:traceback_index]).strip()
                    events.append(self._traceback_event(block, context=context))
                    i = end if end is not None else len(lines)
                    continue
                events.append(ErrorEvent(
                    source=self.name,
                    message=line.strip()[:200],
                    details=line.strip(),
                ))
            i += 1

        return events, tail

    def _collect_traceback(self, lines: list[str], start: int) -> tuple[list[str], int | None]:
        """Collect a traceback starting at lines[start].

        Returns (block, index after block), or (partial, None) if the
        traceback runs to the end of the available lines (still streaming).
        """
        block = [lines[start]]
        i = start + 1
        while i < len(lines):
            line = lines[i]
            # Indented frames, chained-exception headers, and the blank lines
            # Go puts between goroutines all continue the trace.
            if line.startswith((" ", "\t")) or self._is_traceback_start(line) or not line.strip():
                block.append(line)
                i += 1
                continue
            # A Go goroutine header is followed by an unindented function name.
            if block[-1].startswith("goroutine "):
                block.append(line)
                i += 1
                continue
            # First other non-indented line is the exception ("ValueError: ...").
            block.append(line)
            return block, i + 1
        return block, None

    def _traceback_event(self, block: list[str], context: str | None = None) -> ErrorEvent:
        details = "\n".join(block)
        if context:
            details = f"{context.strip()}\n{details}"
        return ErrorEvent(
            source=self.name,
            message=self._traceback_message(block)[:200],
            details=details,
        )

    @staticmethod
    def _traceback_message(block: list[str]) -> str:
        """The one-line summary for a traceback block.

        Headers like "panic:" and "Exception in thread" self-describe the
        error — the exception IS the header. Separator-style headers
        (Traceback, Caused by) put the exception on the last non-indented,
        non-empty line instead.
        """
        header = block[0].strip()
        if header.startswith(_SELF_DESCRIBING_HEADERS[0]) or _SELF_DESCRIBING_HEADERS[1] in header:
            return header
        for line in reversed(block):
            if line.strip() and not line.startswith((" ", "\t")):
                stripped = line.strip()
                if stripped != header:
                    return stripped
        return header
