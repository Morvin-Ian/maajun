from __future__ import annotations

PLACEHOLDER_REPO = "owner/name"


def is_valid_repo(repo: str) -> bool:
    """True if `repo` is a well-formed 'owner/name' slug — exactly one slash,
    with a non-empty owner and name (no leading/trailing slash)."""
    return repo.count("/") == 1 and not repo.startswith("/") and not repo.endswith("/")


def qualify(name: str, owner: str) -> str:
    """A repo name completed with `owner` when it was given without one.

    Once GitHub is authenticated, the account is known — so "myapp" is not
    ambiguous and should not have to be typed as "me/myapp".
    """
    name = name.strip().strip("/")
    if not name or "/" in name or not owner:
        return name
    return f"{owner}/{name}"
