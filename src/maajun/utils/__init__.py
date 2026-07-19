from __future__ import annotations

from datetime import UTC, datetime

GITHUB_API_VERSION = "2022-11-28"


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def github_headers(token: str) -> dict[str, str]:
    """Standard headers for the GitHub REST API."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
