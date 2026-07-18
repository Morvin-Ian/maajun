"""Monitor contract: every error source produces normalized ErrorEvents."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"\d+")


def fingerprint(text: str) -> str:
    """Stable hash of an error, insensitive to line numbers, addresses,
    timestamps, and other volatile digits."""
    normalized = _HEX_RE.sub("", text)
    normalized = _NUM_RE.sub("", normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ErrorEvent:
    source: str  # e.g. "logfile:/var/log/app/error.log"
    message: str  # one-line summary
    details: str  # full traceback / log excerpt
    fingerprint: str = ""
    timestamp: str = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.details)


class Monitor(ABC):
    """An error source the daemon polls."""

    @abstractmethod
    async def poll(self) -> list[ErrorEvent]:
        """Return error events observed since the last poll."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass
