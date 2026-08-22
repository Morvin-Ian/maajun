from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from maajun.monitors.shell import CommandStreamMonitor

log = logging.getLogger(__name__)

UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9_.@-]")


def cursor_path(directory: Path, unit: str) -> Path:
    """Where a unit's journal cursor is kept. Unit names contain / and @."""
    return directory / f"{UNSAFE_IN_FILENAME.sub('_', unit)}.cursor"


class JournaldMonitor(CommandStreamMonitor):
    """Reads a systemd unit's journal: gunicorn, uvicorn, nginx, supervisor.

    `-o cat` because the default format prefixes every line, which un-indents
    a traceback and makes it ungroupable. Position is journalctl's own cursor
    file, so a restart resumes exactly; until it exists a window from startup
    stands in, rather than replaying the whole journal.
    """

    def __init__(
        self,
        unit: str,
        *args,
        cursor_dir: str | Path | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.unit = unit
        self.since = time.time()
        self.pending_since = self.since
        self.cursor_file = self.prepare_cursor(cursor_dir)

    @property
    def name(self) -> str:
        return f"journald:{self.unit}"

    def prepare_cursor(self, cursor_dir: str | Path | None) -> Path | None:
        """The cursor file, or None if its directory cannot be created.

        Falling back to the time window keeps the monitor working on a host
        where the workdir is not writable, rather than failing every poll.
        """
        if cursor_dir is None:
            return None
        directory = Path(cursor_dir).expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning(
                "%s: cannot use a journal cursor in %s (%s); reading by time "
                "window instead", self.name, directory, e,
            )
            return None
        return cursor_path(directory, self.unit)

    def command(self) -> list[str]:
        cmd = ["journalctl", "-u", self.unit, "--no-pager", "-o", "cat"]
        self.pending_since = time.time()
        if self.cursor_file:
            cmd.append(f"--cursor-file={self.cursor_file}")
            # Both flags together would let --since skip entries the cursor
            # says are unread, so the window is only for the first run.
            if self.cursor_file.exists():
                return cmd
        cmd += ["--since", f"@{int(self.since)}"]
        return cmd

    def on_success(self) -> None:
        self.since = self.pending_since
