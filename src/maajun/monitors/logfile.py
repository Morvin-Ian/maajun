from __future__ import annotations

from pathlib import Path

from maajun.monitors.defaults import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    TRACEBACK_LOOKAHEAD_LINES,
)
from maajun.monitors.stream import LogStreamMonitor


class LogFileMonitor(LogStreamMonitor):
    """Incrementally reads a log file, surviving rotation and truncation.

    The reading of the text is inherited; this adds only the file cursor.
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

    @property
    def name(self) -> str:
        return f"logfile:{self.path}"

    async def read_stream(self) -> str:
        # Reading a local file is a syscall or two; not worth a thread.
        return self.read_new()

    def read_new(self) -> str:
        """Read whatever has been appended since the last poll.

        Binary, so the offset is a byte count comparable with st_size, and so
        a UTF-8 log is decoded as UTF-8 rather than as the host's locale.
        TextIOWrapper.tell() returns an opaque cookie, not a position.
        """
        if not self.path.exists():
            return ""
        stat = self.path.stat()
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
            data = f.read()
            self.offset = f.tell()
        # A read can land mid-character while the writer is still going.
        return data.decode("utf-8", errors="replace")
