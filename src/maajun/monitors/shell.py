from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod

from maajun.monitors.stream import LogStreamMonitor
from maajun.utils.commands import COMMAND_TIMEOUT, CommandOutput, run_text

log = logging.getLogger(__name__)

__all__ = ["CommandOutput", "CommandStreamMonitor", "run_text"]


class CommandStreamMonitor(LogStreamMonitor):
    """A log stream read by shelling out to a local command each poll.

    Subclasses build the command; this handles running it off the event loop
    and reporting a broken source once rather than every poll.
    """

    def __init__(self, *args, timeout: float = COMMAND_TIMEOUT, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout
        self.last_error = ""

    @abstractmethod
    def command(self) -> list[str]:
        """The command to run for this poll."""

    def read_output(self, output: CommandOutput) -> str:
        """The log text in a successful result. Override to include stderr."""
        return output.stdout

    def on_success(self) -> None:
        """Called after a read that worked, to commit any window advance."""

    async def read_stream(self) -> str:
        # subprocess.run blocks, and poll_once gathers every monitor at once.
        return await asyncio.to_thread(self.read_now)

    def read_now(self) -> str:
        output = run_text(self.command(), timeout=self.timeout)
        if output.error:
            # Once per distinct message: a stopped container or a missing
            # binary would otherwise log on every poll, forever.
            if output.error != self.last_error:
                log.warning("%s: %s", self.name, output.error)
                self.last_error = output.error
            return ""
        if self.last_error:
            log.info("%s: reading again", self.name)
            self.last_error = ""
        self.on_success()
        return self.read_output(output)
