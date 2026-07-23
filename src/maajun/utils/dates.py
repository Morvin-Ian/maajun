from __future__ import annotations

from datetime import UTC, datetime


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")
