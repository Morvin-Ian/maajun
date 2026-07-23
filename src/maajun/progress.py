"""Shared animated terminal status indicators.

Two live spinners built on Rich: `ThinkingStatus` for the chat stream (rotates
through playful words while the model reasons) and `WorkingStatus` for
multi-phase background tasks like `report` and `watch` (shows the current phase
and how long it has been running).
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

THINKING_WORDS = [
    "Thinking", "Reasoning", "Pondering", "Cogitating", "Mulling",
    "Musing", "Percolating", "Brewing", "Ruminating", "Deliberating",
    "Puzzling", "Noodling", "Marinating", "Untangling",
    "Connecting dots", "Weighing options", "Piecing it together",
]
_WORD_SECONDS = 2.5


class ThinkingStatus:
    """Animated 'Thinking… (3s)' line shown instead of the reasoning text.

    Re-renders on every Live refresh, so the spinner, the elapsed timer, and
    the periodic word rotation all keep moving even while the stream stalls.
    """

    def __init__(self):
        self._started = time.monotonic()
        self._word = random.choice(THINKING_WORDS)
        self._word_at = self._started
        self._spinner = Spinner("dots", style="dim")

    def __rich_console__(self, console, options):
        now = time.monotonic()
        if now - self._word_at >= _WORD_SECONDS:
            self._word = random.choice([w for w in THINKING_WORDS if w != self._word])
            self._word_at = now
        elapsed = int(now - self._started)
        self._spinner.update(text=Text(f"{self._word}… ({elapsed}s)", style="dim"))
        yield self._spinner


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
def working(console: Console, phase: str) -> Iterator[WorkingStatus]:
    """Show a transient 'working' spinner until the block exits.

    Yields the status so the caller can advance the phase label. The spinner
    clears on exit so the final result prints cleanly beneath it.
    """
    status = WorkingStatus(phase)
    with Live(status, console=console, refresh_per_second=8, transient=True):
        yield status
