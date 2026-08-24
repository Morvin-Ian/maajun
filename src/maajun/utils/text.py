from __future__ import annotations


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Return `text` capped at `limit` characters, appending `suffix` if it
    was actually shortened. Text within the limit is returned unchanged."""
    if len(text) <= limit:
        return text
    return text[:limit] + suffix


def truncate_tail(text: str, limit: int, prefix: str = "…") -> str:
    """Return the *last* `limit` characters of `text`, prefixed if shortened.

    For command output the end is what matters: pytest, npm, go test and cargo
    all print what failed last. Cutting from the front keeps the collection
    noise and drops the reason.
    """
    if len(text) <= limit:
        return text
    return prefix + text[-limit:]
