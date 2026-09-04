import pytest

from maajun.cli.promotion import (
    PromotionTarget,
    configured_repo,
    resolve_promotion,
    validate_issue,
)
from maajun.config import Config, GitHubConfig, RepoConfig
from maajun.daemon.store import ARTIFACT_ISSUE, ARTIFACT_PR, IncidentStore
from maajun.monitors import ErrorEvent
from maajun.vcs import GitHubError, GitHubIssue


@pytest.fixture
def store(tmp_path):
    database = IncidentStore(tmp_path / "incidents.db")
    yield database
    database.close()


def record_issue(store, fingerprint, repo, number):
    event = ErrorEvent(
        source="log",
        message="broken",
        details=fingerprint,
        fingerprint=fingerprint,
        repo=repo,
    )
    store.record(event)
    store.mark_processed(
        fingerprint,
        repo,
        branch="",
        pr_url=f"https://github.com/{repo}/issues/{number}",
        artifact_kind=ARTIFACT_ISSUE,
    )


def test_resolves_a_recorded_issue_by_fingerprint_url_or_number(store):
    record_issue(store, "abc123456789", "owner/name", 12)

    assert resolve_promotion(store, "abc123").issue_number == 12
    assert resolve_promotion(
        store, "https://github.com/owner/name/issues/12"
    ).fingerprint == "abc123456789"
    assert resolve_promotion(store, "12", "owner/name").repo == "owner/name"


def test_rejects_an_issue_maajun_did_not_record(store):
    with pytest.raises(ValueError, match="No recorded Maajun issue"):
        resolve_promotion(store, "https://github.com/owner/name/issues/99")


def test_rejects_an_ambiguous_fingerprint(store):
    record_issue(store, "shared-one", "owner/api", 1)
    record_issue(store, "shared-two", "owner/web", 2)

    with pytest.raises(ValueError, match="ambiguous"):
        resolve_promotion(store, "shared")


def test_issue_number_requires_a_repo_when_several_repos_have_incidents(store):
    record_issue(store, "api", "owner/api", 1)
    record_issue(store, "web", "owner/web", 1)

    with pytest.raises(ValueError, match="needs --repo"):
        resolve_promotion(store, "1")


def test_pull_request_records_are_not_promotion_candidates(store):
    event = ErrorEvent(
        source="log", message="fixed", details="fixed", fingerprint="fixed", repo="owner/name"
    )
    store.record(event)
    store.mark_processed(
        "fixed",
        "owner/name",
        branch="maajun/incident-fixed",
        pr_url="https://github.com/owner/name/pull/3",
        artifact_kind=ARTIFACT_PR,
    )

    with pytest.raises(ValueError, match="No recorded Maajun issue"):
        resolve_promotion(store, "fixed")


def test_rejects_an_unconfigured_repository():
    config = Config(github=GitHubConfig(repos=[RepoConfig(repo="owner/other")]))

    with pytest.raises(ValueError, match="is not configured"):
        configured_repo(config, "owner/name")


def test_rejects_a_closed_recorded_issue():
    target = PromotionTarget(
        "recorded-fingerprint",
        "owner/name",
        "https://github.com/owner/name/issues/12",
        12,
    )
    issue = GitHubIssue(
        12,
        "Already resolved",
        "body",
        "https://github.com/owner/name/issues/12",
        "closed",
    )

    with pytest.raises(GitHubError, match="closed; reopen it"):
        validate_issue(issue, target)


def test_rejects_a_different_issue_returned_by_github():
    target = PromotionTarget(
        "recorded-fingerprint",
        "owner/name",
        "https://github.com/owner/name/issues/12",
        12,
    )
    issue = GitHubIssue(
        12,
        "Wrong issue",
        "body",
        "https://github.com/other/name/issues/12",
        "open",
    )

    with pytest.raises(GitHubError, match="different issue"):
        validate_issue(issue, target)
