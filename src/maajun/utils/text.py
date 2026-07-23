from __future__ import annotations


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Return `text` capped at `limit` characters, appending `suffix` if it
    was actually shortened. Text within the limit is returned unchanged."""
    if len(text) <= limit:
        return text
    return text[:limit] + suffix
