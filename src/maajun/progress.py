"""Animated terminal status indicator for multi-phase background tasks.

A Rich live spinner used by `report` and `watch` to show the current phase
and how long it has been running.
"""

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
        self._phase = phase
        self._started = time.monotonic()
        self._spinner = Spinner("dots", style="cyan")

    def set(self, phase: str) -> None:
        if phase != self._phase:
            self._phase = phase
            self._started = time.monotonic()

    def __rich_console__(self, console, options):
        elapsed = int(time.monotonic() - self._started)
        self._spinner.update(text=Text(f"{self._phase}… ({elapsed}s)", style="cyan"))
        yield self._spinner


@contextmanager
def working(console: Console, phase: str) -> Generator[WorkingStatus, None, None]:
    """Show a transient 'working' spinner until the block exits.

    Yields the status so the caller can advance the phase label. The spinner
    clears on exit so the final result prints cleanly beneath it.
    """
    status = WorkingStatus(phase)
    with Live(status, console=console, refresh_per_second=8, transient=True):
        yield status
