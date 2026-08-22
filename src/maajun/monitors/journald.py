from __future__ import annotations

import logging
import time
from pathlib import Path

from maajun.monitors.cursors import cursor_path, usable
from maajun.monitors.shell import CommandStreamMonitor

log = logging.getLogger(__name__)


class JournaldMonitor(CommandStreamMonitor):
    """Reads a systemd unit's journal: gunicorn, uvicorn, nginx, supervisor.

    `-o cat` because the default format prefixes every line, which un-indents
    a traceback and makes it ungroupable. Position is journalctl's own cursor
    file, so a restart resumes exactly; until it exists a window from startup
    stands in, rather than replaying the whole journal — unless `backfill`
    asks for exactly that.
    """

    def __init__(
        self,
        unit: str,
        *args,
        cursor_dir: str | Path | None = None,
        backfill: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.unit = unit
        self.backfill = backfill
        self.since = time.time()
        self.pending_since = self.since
        self.read_once = False
        directory = usable(cursor_dir, self.name)
        self.cursor_file = cursor_path(directory, unit) if directory else None
        if backfill and self.cursor_file:
            # Otherwise the cursor — the end of the journal after one
            # ordinary run — would win and backfill would read nothing.
            self.cursor_file.unlink(missing_ok=True)

    @property
    def name(self) -> str:
        return f"journald:{self.unit}"

    def command(self) -> list[str]:
        cmd = ["journalctl", "-u", self.unit, "--no-pager", "-o", "cat"]
        self.pending_since = time.time()
        if self.cursor_file:
            cmd.append(f"--cursor-file={self.cursor_file}")
            # Both flags together would let --since skip entries the cursor
            # says are unread, so the window is only for the first run.
            if self.cursor_file.exists():
                return cmd
        if self.backfill and not self.read_once:
            # Everything the journal still holds for this unit. Guarded by
            # read_once as well as the cursor, since without a writable
            # cursor directory every poll would replay the lot.
            return cmd
        cmd += ["--since", f"@{int(self.since)}"]
        return cmd

    def on_success(self) -> None:
        self.since = self.pending_since
        self.read_once = True
