"""Tail a log file and emit ErrorEvents for tracebacks and error lines."""

from __future__ import annotations

import re
from pathlib import Path

from maajun.monitors.base import ErrorEvent, Monitor

DEFAULT_ERROR_PATTERN = r"\b(ERROR|CRITICAL|FATAL)\b"
TRACEBACK_HEADER = "Traceback (most recent call last):"

# Cap on carried-over text for tracebacks split across polls.
MAX_PENDING = 64 * 1024


class LogFileMonitor(Monitor):
    """Incrementally reads a log file, surviving rotation and truncation.

    Emits one event per error: an ERROR line immediately followed by a
    traceback (the logging.exception pattern) is merged into a single
    event rather than two. Text that might still be streaming in (an
    unterminated traceback, or an ERROR line that may have a traceback
    right behind it) is carried over and flushed on the next quiet poll.
    """

    def __init__(self, path: str | Path, error_pattern: str = DEFAULT_ERROR_PATTERN):
        self.path = Path(path).expanduser()
        self.error_re = re.compile(error_pattern)
        self._offset = 0
        self._inode: int | None = None
        self._pending = ""

    @property
    def name(self) -> str:
        return f"logfile:{self.path}"

    async def poll(self) -> list[ErrorEvent]:
        text = self._read_new()
        if not text:
            if self._pending:
                # Nothing new for a whole interval — what we held back is
                # not still streaming; emit it.
                events, _ = self._parse(self._pending, flush=True)
                self._pending = ""
                return events
            return []
        events, self._pending = self._parse(self._pending + text)
        if len(self._pending) > MAX_PENDING:
            self._pending = self._pending[-MAX_PENDING:]
        return events

    def _read_new(self) -> str:
        if not self.path.exists():
            return ""
        stat = self.path.stat()
        rotated = self._inode is not None and stat.st_ino != self._inode
        truncated = stat.st_size < self._offset
        if rotated or truncated:
            self._offset = 0
            self._pending = ""
        self._inode = stat.st_ino

        if stat.st_size == self._offset:
            return ""
        with open(self.path, errors="replace") as f:
            f.seek(self._offset)
            text = f.read()
            self._offset = f.tell()
        return text

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

            if TRACEBACK_HEADER in line:
                block, end = self._collect_traceback(lines, i)
                if end is None and not flush:
                    return events, "\n".join(lines[i:] + [tail])
                events.append(self._traceback_event(block))
                i = end if end is not None else len(lines)
                continue

            if self.error_re.search(line):
                nxt = i + 1
                if nxt == len(lines) and not flush:
                    # A traceback may be right behind this line; wait one poll.
                    return events, "\n".join([line, tail])
                if nxt < len(lines) and TRACEBACK_HEADER in lines[nxt]:
                    block, end = self._collect_traceback(lines, nxt)
                    if end is None and not flush:
                        return events, "\n".join(lines[i:] + [tail])
                    events.append(self._traceback_event(block, context_line=line))
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
            if line.startswith((" ", "\t")) or TRACEBACK_HEADER in line:
                block.append(line)
                i += 1
                continue
            # First non-indented line is the exception line ("ValueError: ...").
            block.append(line)
            return block, i + 1
        return block, None

    def _traceback_event(self, block: list[str], context_line: str | None = None) -> ErrorEvent:
        exception_line = block[-1].strip()
        details = "\n".join(block)
        if context_line:
            details = f"{context_line.strip()}\n{details}"
        return ErrorEvent(
            source=self.name,
            message=exception_line[:200],
            details=details,
        )
