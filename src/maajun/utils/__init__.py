"""Small, dependency-light helpers shared across the codebase.

Grouped into themed modules (dates, repos, text) and re-exported here so
callers can keep importing straight from `maajun.utils`.
"""

from __future__ import annotations

from maajun.utils.commands import CommandOutput, run_text
from maajun.utils.dates import utc_day_start_iso, utcnow_iso
from maajun.utils.repos import PLACEHOLDER_REPO, is_valid_repo
from maajun.utils.text import truncate

__all__ = [
    "CommandOutput",
    "PLACEHOLDER_REPO",
    "is_valid_repo",
    "run_text",
    "truncate",
    "utc_day_start_iso",
    "utcnow_iso",
]
