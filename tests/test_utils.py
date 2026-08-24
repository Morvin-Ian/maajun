import pytest

from maajun.utils import (
    PLACEHOLDER_REPO,
    is_valid_repo,
    truncate,
    truncate_tail,
    utcnow_iso,
)
from maajun.vcs.api import GITHUB_API_VERSION, github_headers

# --- text.truncate ---------------------------------------------------------

def test_truncate_leaves_short_text_untouched():
    assert truncate("hello", 10) == "hello"
    assert truncate("hello", 5) == "hello"  # exactly at the limit


def test_truncate_appends_suffix_when_cut():
    assert truncate("hello world", 5, suffix="…") == "hello…"
    assert truncate("abcdef", 3, suffix="...") == "abc..."


def test_truncate_default_suffix_is_ellipsis():
    assert truncate("abcdef", 3) == "abc…"


# --- repos -----------------------------------------------------------------

@pytest.mark.parametrize("repo", ["owner/name", "a/b", "org-1/repo.py"])
def test_valid_repos(repo):
    assert is_valid_repo(repo) is True


@pytest.mark.parametrize("repo", ["", "name", "a/b/c", "/name", "owner/", "/"])
def test_invalid_repos(repo):
    assert is_valid_repo(repo) is False


def test_placeholder_repo_is_valid_shape_but_conventional():
    assert PLACEHOLDER_REPO == "owner/name"


# --- re-exported helpers still importable from the package root ------------

def test_utcnow_iso_roundtrips():
    from datetime import datetime

    stamp = utcnow_iso()
    # Parses as ISO-8601 and carries a timezone.
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None


def test_github_headers():
    headers = github_headers("tok")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION


def test_truncate_tail_keeps_the_end_of_long_output():
    """A test runner prints what failed last. Cutting from the front spends
    the budget on collection noise and drops the reason."""
    output = "collecting" * 500 + "\nFAILED tests/test_cart.py::test_total"

    kept = truncate_tail(output, 60)

    assert kept.endswith("FAILED tests/test_cart.py::test_total")
    assert kept.startswith("…")
    assert len(kept) == 61


def test_truncate_tail_leaves_short_output_alone():
    assert truncate_tail("all good", 60) == "all good"
