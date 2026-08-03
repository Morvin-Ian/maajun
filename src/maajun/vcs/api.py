"""GitHub REST API conventions, shared by the client and the Actions monitor."""

from __future__ import annotations

GITHUB_API_VERSION = "2022-11-28"


def github_headers(token: str) -> dict[str, str]:
    """Standard headers for the GitHub REST API."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
