from __future__ import annotations

from datetime import UTC, datetime


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def utc_day_start_iso() -> str:
    """Start of the current UTC day, in the same format as utcnow_iso().

    Comparable with stored timestamps as a plain string, since ISO-8601 with a
    fixed offset sorts lexicographically.
    """
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat(timespec="seconds")
