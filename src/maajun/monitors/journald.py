from __future__ import annotations

import logging
import time
from pathlib import Path

from maajun.monitors.cursors import cursor_path, usable
from maajun.monitors.shell import CommandOutput, CommandStreamMonitor

log = logging.getLogger(__name__)

# How far back --backfill reaches. Unbounded, one call buffers the unit's
# whole journal into memory.
BACKFILL_LINES = 50_000


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
            if self.cursor_file.exists():
                return [*cmd, f"--cursor-file={self.cursor_file}"]
            # --cursor-file and --since are mutually exclusive. Ask
            # journalctl to print the cursor after the initial time window,
            # then persist it in read_output for later polls.
            cmd.append("--show-cursor")
        if self.backfill and not self.read_once:
            # The newest BACKFILL_LINES the journal still holds for this
            # unit. Guarded by read_once as well as the cursor, since without
            # a writable cursor directory every poll would replay the lot.
            return [*cmd, "-n", str(BACKFILL_LINES)]
        cmd += ["--since", f"@{int(self.since)}"]
        return cmd

    def read_output(self, output: CommandOutput) -> str:
        text = super().read_output(output)
        if not self.cursor_file or self.cursor_file.exists():
            return text

        lines = text.splitlines(keepends=True)
        last_content = next(
            (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
            None,
        )
        if last_content is None:
            return text

        prefix = "-- cursor: "
        marker = lines[last_content].strip()
        if not marker.startswith(prefix):
            return text

        try:
            self.cursor_file.write_text(marker.removeprefix(prefix))
        except OSError as error:
            log.debug("could not write %s: %s", self.cursor_file, error)
        del lines[last_content]
        return "".join(lines)

    def on_success(self) -> None:
        self.since = self.pending_since
        self.read_once = True
