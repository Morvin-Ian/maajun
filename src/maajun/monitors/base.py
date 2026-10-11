from __future__ import annotations

import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from maajun.privacy import canonical_evidence, sanitize_evidence
from maajun.utils import utcnow_iso

log = logging.getLogger(__name__)

HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
NUM_RE = re.compile(r"\d+")

# Shared, so every monitor's fingerprint is the same width.
FINGERPRINT_LENGTH = 16

NGINX_ERROR_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[(?:error|crit|alert|emerg)\] "
    r"\d+#\d+: (?:\*\d+ )?"
)
NGINX_SERVER_RE = re.compile(r'(?:^|, )server: ([^,\s]+)')
NGINX_UPSTREAM_RE = re.compile(r', upstream: "([^"\r\n]+)"')


def nginx_connection_identity(text: str) -> str | None:
    """A refused upstream is one outage across client IPs and request paths.

    Keep upstream host/port and server identity before privacy redaction; removing
    every number would merge outages on different services on the same host.
    Restrict this to connection failures: route-specific application errors must
    retain their normal fingerprint.
    """
    prefix = NGINX_ERROR_RE.match(text)
    server = NGINX_SERVER_RE.search(text)
    upstream = NGINX_UPSTREAM_RE.search(text)
    if not prefix or not server or not upstream:
        return None
    reason = text[prefix.end():].partition(", client:")[0]
    if not re.fullmatch(r"connect\(\) failed \([^\r\n]+\) while connecting to upstream", reason):
        return None
    try:
        target = urlsplit(upstream[1])
        if target.scheme not in {"http", "https"} or not target.hostname:
            return None
        port = target.port or (443 if target.scheme == "https" else 80)
    except ValueError:
        return None
    return repr(("nginx-connect", reason, server[1].lower().rstrip("."),
                 target.scheme, target.hostname.lower(), port))


def fingerprint(text: str) -> str:
    identity = nginx_connection_identity(text)
    if identity is not None:
        return hashlib.sha256(identity.encode()).hexdigest()[:FINGERPRINT_LENGTH]
    normalized = canonical_evidence(text)
    # A query may be present on one client version and omitted on another, and
    # its values are already redacted — noise for an error on the same route.
    normalized = re.sub(r"(?P<path>/[^\s\"'?]+)\?[^\s\"']+", r"\g<path>", normalized)
    normalized = HEX_RE.sub("", normalized)
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
        self.source = sanitize_evidence(self.source)
        self.message = sanitize_evidence(self.message)
        self.details = sanitize_evidence(self.details)


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
