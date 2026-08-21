from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text


class WorkingStatus:
    """Animated 'Analyzing with AI… (12s)' line for a multi-phase task.

    The phase label is mutable via `set`; the elapsed timer resets on each
    phase change so every step shows its own duration.
    """

    def __init__(self, phase: str):
        self.phase = phase
        self.started = time.monotonic()
        self.spinner = Spinner("dots", style="cyan")
        self.live: Live | None = None

    def set(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.started = time.monotonic()

    @contextmanager
    def paused(self) -> Generator[None, None, None]:
        """Take the spinner off the screen while something else reads input. """
        if self.live is None:
            yield
            return
        self.live.stop()
        try:
            yield
        finally:
            self.live.start()

    def __rich_console__(self, console, options):
        elapsed = int(time.monotonic() - self.started)
        self.spinner.update(text=Text(f"{self.phase}… ({elapsed}s)", style="cyan"))
        yield self.spinner


@contextmanager
def working(console: Console, phase: str) -> Generator[WorkingStatus, None, None]:
    """Show a transient 'working' spinner until the block exits. """
    status = WorkingStatus(phase)
    with Live(status, console=console, refresh_per_second=8, transient=True) as live:
        status.live = live
        yield status
