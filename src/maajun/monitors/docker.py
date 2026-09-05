from __future__ import annotations

import time

from maajun.monitors.shell import CommandOutput, CommandStreamMonitor

# How far back --backfill reaches. Unbounded, `docker logs` buffers the
# container's whole retained log.
BACKFILL_LINES = 50_000


class DockerLogMonitor(CommandStreamMonitor):
    """Reads a container's logs, in docker or compose.

    A time window per poll, not a follow, so a poll cannot block. No `-t`:
    the prefix would un-indent tracebacks. A boundary may repeat a line,
    which costs nothing — the store dedups by fingerprint.
    """

    def __init__(self, container: str, *args, backfill: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.container = container
        self.backfill = backfill
        self.since = time.time()
        self.pending_since = self.since
        self.read_once = False

    @property
    def name(self) -> str:
        return f"docker:{self.container}"

    def command(self) -> list[str]:
        self.pending_since = time.time()
        if self.backfill and not self.read_once:
            # The newest BACKFILL_LINES it still holds — once, then windows.
            return [
                "docker", "logs", "--tail", str(BACKFILL_LINES), self.container
            ]
        return [
            "docker", "logs", "--since", f"{int(self.since)}", self.container
        ]

    def read_output(self, output: CommandOutput) -> str:
        # An unhandled exception goes to stderr, so stdout alone would miss
        # every traceback. read_now keeps a failed command from reaching here.
        return "\n".join(part for part in (output.stdout, output.stderr) if part)

    def on_success(self) -> None:
        self.since = self.pending_since
        self.read_once = True
