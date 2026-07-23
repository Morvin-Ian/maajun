from __future__ import annotations

GITHUB_API_VERSION = "2022-11-28"

# Shown as the example/unset repository throughout the CLI and starter config.
PLACEHOLDER_REPO = "owner/name"


def github_headers(token: str) -> dict[str, str]:
    """Standard headers for the GitHub REST API."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def is_valid_repo(repo: str) -> bool:
    """True if `repo` is a well-formed 'owner/name' slug — exactly one slash,
    with a non-empty owner and name (no leading/trailing slash)."""
    return repo.count("/") == 1 and not repo.startswith("/") and not repo.endswith("/")
