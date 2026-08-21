from __future__ import annotations

import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from maajun.utils import utcnow_iso

log = logging.getLogger(__name__)

HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
NUM_RE = re.compile(r"\d+")

# Characters of the sha256 digest an incident is keyed by. Shared so a monitor
# building its own key matches the width of every other fingerprint.
FINGERPRINT_LENGTH = 16

# Cap on remembered item ids. The APIs return only the most recent items
# per poll, so a bounded window is enough to dedup — and it stops seen from
# growing without limit over a long-running daemon.
MAX_SEEN_IDS = 5000

def fingerprint(text: str) -> str:
    normalized = HEX_RE.sub("", text)
    normalized = NUM_RE.sub("", normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:FINGERPRINT_LENGTH]


@dataclass
class ErrorEvent:
    source: str  # e.g. "logfile:/var/log/app/error.log"
    message: str  # one-line summary
    details: str  # full traceback / log excerpt
    fingerprint: str = ""
    timestamp: str = field(default_factory=utcnow_iso)
    repo: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.details)


class Monitor(ABC):
    """An error source the daemon polls."""

    def __init__(self, *, burst_threshold: int = 1, burst_window_seconds: float = 60.0):
        """
        burst_threshold: emit nothing until this many events land inside
            burst_window_seconds. 1 (the default) emits every event
            immediately. Use it to ignore one-off blips and only report
            an error that is actually repeating.
        burst_window_seconds: how long a held event stays eligible.
        """
        self.burst_threshold = max(1, burst_threshold)
        self.burst_window_seconds = burst_window_seconds
        self.burst_buffer: deque[tuple[float, ErrorEvent]] = deque()

    @abstractmethod
    async def poll(self) -> list[ErrorEvent]:
        """Return error events observed since the last poll."""

    async def flush(self) -> list[ErrorEvent]:
        """Flush any carried-over state and return remaining events.

        Called before the daemon exits in --once mode to ensure no
        pending errors are lost — including a burst that never reached
        its threshold.
        """
        return self.drain_burst_buffer()

    def apply_burst_threshold(self, events: list[ErrorEvent]) -> list[ErrorEvent]:
        """Return the events to emit now, holding back an incomplete burst.

        With thresholding off this is the identity. Otherwise events are
        buffered until `burst_threshold` of them are in the window, at which
        point the *whole* buffered burst is emitted — not just the batch that
        happened to cross the line.
        """
        if self.burst_threshold <= 1:
            return events
        self.hold_for_burst(events)
        if len(self.burst_buffer) < self.burst_threshold:
            return []
        return self.drain_burst_buffer()

    def hold_for_burst(self, events: list[ErrorEvent]) -> None:
        """Buffer events and drop any that have aged out of the window.

        Timed on the monotonic clock: an NTP step should not retroactively
        expire a window on a long-running daemon.
        """
        now = time.monotonic()
        self.burst_buffer.extend((now, event) for event in events)
        cutoff = now - self.burst_window_seconds
        while self.burst_buffer and self.burst_buffer[0][0] < cutoff:
            self.burst_buffer.popleft()

    def drain_burst_buffer(self) -> list[ErrorEvent]:
        events = [event for _, event in self.burst_buffer]
        self.burst_buffer.clear()
        return events

    @property
    @abstractmethod
    def name(self) -> str:
        pass


class HTTPPollMonitor(Monitor):
    """Base for monitors that poll an HTTP API and dedup items by id.

    Subclasses implement fetch/item_id/to_event; poll() handles the
    fetch-failure logging and seen-id bookkeeping.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        burst_threshold: int = 1,
        burst_window_seconds: float = 60.0,
    ):
        super().__init__(
            burst_threshold=burst_threshold,
            burst_window_seconds=burst_window_seconds,
        )
        self.client = client
        # OrderedDict as an insertion-ordered set, so the oldest ids can be
        # evicted once the window is full.
        self.seen: OrderedDict[str, None] = OrderedDict()

    async def poll(self) -> list[ErrorEvent]:
        try:
            items = await self.fetch()
        except Exception:
            log.exception("%s: failed to fetch", self.name)
            return []

        events: list[ErrorEvent] = []
        for item in items:
            item_id = self.item_id(item)
            if item_id in self.seen:
                continue
            self.seen[item_id] = None
            events.append(self.to_event(item))

        while len(self.seen) > MAX_SEEN_IDS:
            self.seen.popitem(last=False)

        return self.apply_burst_threshold(events)

    async def aclose(self) -> None:
        await self.client.aclose()

    @abstractmethod
    async def fetch(self) -> list[dict[str, Any]]:
        """Return the raw items from the API."""

    @abstractmethod
    def item_id(self, item: dict[str, Any]) -> str:
        """Stable id used to skip already-seen items across polls."""

    @abstractmethod
    def to_event(self, item: dict[str, Any]) -> ErrorEvent:
        """Convert one raw item into an ErrorEvent."""
