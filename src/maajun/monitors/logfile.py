from __future__ import annotations

import asyncio
from pathlib import Path

from maajun.monitors.cursors import (
    Position,
    cursor_path,
    read_position,
    usable,
    write_position,
)
from maajun.monitors.defaults import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    TRACEBACK_LOOKAHEAD_LINES,
)
from maajun.monitors.stream import LogStreamMonitor

# Most one poll will read. An increment is never near this; a backfill is,
# and the rest of it is read by the next poll.
MAX_READ_BYTES = 16 * 1024 * 1024

# Past this a read is worth a thread.
THREAD_ABOVE_BYTES = 512 * 1024


class LogFileMonitor(LogStreamMonitor):
    """Incrementally reads a log file, surviving rotation and truncation.

    The reading of the text is inherited; this adds only the file cursor.
    Where that cursor starts is the whole question for a log that already
    exists — see `position`.
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
        cursor_dir: str | Path | None = None,
        backfill: bool = False,
    ):
        super().__init__(
            error_pattern,
            json_level_field=json_level_field,
            json_level_values=json_level_values,
            traceback_headers=traceback_headers,
            traceback_lookahead=traceback_lookahead,
            burst_threshold=burst_threshold,
            burst_window_seconds=burst_window_seconds,
        )
        self.path = Path(path).expanduser()
        self.offset = 0
        self.inode: int | None = None
        self.backfill = backfill
        directory = usable(cursor_dir, self.name)
        self.cursor_file = (
            cursor_path(directory, str(self.path), ".offset") if directory else None
        )
        # Where "from now on" starts, measured now rather than at the first
        # poll: anything written in between still has to be read.
        self.start_at = self.size_now() if not backfill else 0
        self.positioned = False

    @property
    def name(self) -> str:
        return f"logfile:{self.path}"

    async def read_stream(self) -> str:
        # An increment is a syscall or two; a backfill would hold the loop
        # while every other monitor waits.
        if self.pending_bytes() > THREAD_ABOVE_BYTES:
            return await asyncio.to_thread(self.read_new)
        return self.read_new()

    def pending_bytes(self) -> int:
        """Roughly how much is waiting to be read.

        Only chooses between a thread and an inline read, so a rough answer
        before the cursor is positioned is fine.
        """
        size = self.size_now()
        offset = self.offset if self.positioned else min(self.start_at, size)
        return max(0, size - offset)

    def size_now(self) -> int:
        """The file's size, or 0 if it is not there yet — everything a file
        created later holds arrived while maajun was watching."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def position(self, stat) -> None:
        """Choose where to start reading, on the first sight of the file.

        A saved cursor wins: a restart carries on exactly where it stopped,
        without re-reading a log that may be gigabytes. A stale one means the
        file rotated while we were down, so what is there now has never been
        read.

        Otherwise reading starts where the file ended when maajun was asked
        to watch. What was already in it happened before that, and filing an
        issue for each is not what starting a monitor means — `backfill` is
        how you ask for exactly that, and it discards the saved position,
        since after one ordinary run that position is the end of the file.
        """
        self.positioned = True
        if self.backfill:
            self.offset = 0
            return
        saved = read_position(self.cursor_file)
        if saved and saved.inode == stat.st_ino and saved.offset <= stat.st_size:
            self.offset = saved.offset
        elif saved:
            # Rotated or truncated while we were down: none of what the file
            # holds now has been read.
            self.offset = 0
        else:
            self.offset = min(self.start_at, stat.st_size)

    def remember(self) -> None:
        if self.inode is not None:
            write_position(self.cursor_file, Position(self.inode, self.offset))

    def read_new(self) -> str:
        """Read whatever has been appended since the last poll.

        Binary, so the offset is a byte count comparable with st_size, and so
        a UTF-8 log is decoded as UTF-8 rather than as the host's locale.
        TextIOWrapper.tell() returns an opaque cookie, not a position.
        """
        if not self.path.exists():
            return ""
        stat = self.path.stat()
        if not self.positioned:
            self.position(stat)
            self.inode = stat.st_ino
            self.remember()

        rotated = self.inode is not None and stat.st_ino != self.inode
        truncated = stat.st_size < self.offset
        if rotated or truncated:
            self.offset = 0
            self.carryover_text = ""
        self.inode = stat.st_ino

        if stat.st_size <= self.offset:
            return ""
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            # Capped, so a backfill drains over several polls. A read that
            # stops mid-line is what carryover_text is for.
            data = f.read(MAX_READ_BYTES)
            self.offset = f.tell()
        self.remember()
        # A read can land mid-character while the writer is still going.
        return data.decode("utf-8", errors="replace")
