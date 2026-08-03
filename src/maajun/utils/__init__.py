"""Small, dependency-light helpers shared across the codebase.

Grouped into themed modules (dates, github, text, repos) and re-exported here
so callers can keep importing straight from `maajun.utils`.
"""

from __future__ import annotations

from maajun.utils.dates import utc_day_start_iso, utcnow_iso
from maajun.utils.github import (
    GITHUB_API_VERSION,
    PLACEHOLDER_REPO,
    github_headers,
    is_valid_repo,
)
from maajun.utils.text import truncate

__all__ = [
    "GITHUB_API_VERSION",
    "PLACEHOLDER_REPO",
    "github_headers",
    "is_valid_repo",
    "truncate",
    "utc_day_start_iso",
    "utcnow_iso",
]
