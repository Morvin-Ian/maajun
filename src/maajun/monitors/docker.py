from __future__ import annotations

import time

from maajun.monitors.shell import CommandOutput, CommandStreamMonitor


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
            # Everything the container still holds — once, then windows.
            return ["docker", "logs", self.container]
        return [
            "docker", "logs", "--since", f"{int(self.since)}", self.container
        ]

    def read_output(self, output: CommandOutput) -> str:
        # docker relays the container's stderr as its own, and that is where
        # an unhandled exception goes; reading stdout alone would miss every
        # traceback. A failed command never reaches here (see read_now), so
        # this cannot mistake "No such container" for log text.
        return "\n".join(part for part in (output.stdout, output.stderr) if part)

    def on_success(self) -> None:
        self.since = self.pending_since
        self.read_once = True
