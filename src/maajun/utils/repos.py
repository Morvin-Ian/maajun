from __future__ import annotations

PLACEHOLDER_REPO = "owner/name"


def is_valid_repo(repo: str) -> bool:
    """True if `repo` is a well-formed 'owner/name' slug — exactly one slash,
    with a non-empty owner and name (no leading/trailing slash)."""
    return repo.count("/") == 1 and not repo.startswith("/") and not repo.endswith("/")
