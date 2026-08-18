"""Tests for the GitHub Actions monitor."""

import httpx
import pytest

from maajun.monitors.github_actions import GitHubActionsMonitor

FAKE_RUN = {
    "id": "67890",
    "name": "CI",
    "run_number": 42,
    "head_branch": "main",
    "event": "push",
    "conclusion": "failure",
    "html_url": "https://github.com/owner/name/actions/runs/67890",
    "head_sha": "abc12345def67890",
}


@pytest.fixture
def monitor():
    return GitHubActionsMonitor(token="ghp_test", repo="owner/name")


def test_name(monitor):
    assert monitor.name == "gh-actions:owner/name"


@pytest.mark.asyncio
async def test_poll_returns_events(monitor, monkeypatch):
    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"workflow_runs": [FAKE_RUN]}

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert len(events) == 1
    event = events[0]
    assert event.source == "gh-actions:owner/name"
    assert "CI" in event.message
    assert "#42" in event.message
    # Hashed, like every other fingerprint — not the raw 40-char sha.
    assert len(event.fingerprint) == 16
    assert "main" in event.details
    assert "failure" in event.details


@pytest.mark.asyncio
async def test_poll_deduplicates(monitor, monkeypatch):
    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"workflow_runs": [FAKE_RUN]}

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events1 = await monitor.poll()
    events2 = await monitor.poll()

    assert len(events1) == 1
    assert len(events2) == 0


@pytest.mark.asyncio
async def test_poll_empty_response(monitor, monkeypatch):
    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"workflow_runs": []}

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert events == []


@pytest.mark.asyncio
async def test_poll_api_error_returns_empty(monitor, monkeypatch):
    async def mock_get(url, params=None):
        raise httpx.HTTPStatusError("403", request=None, response=httpx.Response(403))

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert events == []


@pytest.mark.asyncio
async def test_poll_multiple_runs(monitor, monkeypatch):
    runs = [
        {**FAKE_RUN, "id": "1", "name": "Lint", "run_number": 1},
        {**FAKE_RUN, "id": "2", "name": "Test", "run_number": 2},
        {**FAKE_RUN, "id": "3", "name": "Deploy", "run_number": 3},
    ]

    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"workflow_runs": runs}

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert len(events) == 3
    names = {e.message for e in events}
    assert "Lint" in str(names)
    assert "Test" in str(names)
    assert "Deploy" in str(names)


def test_run_to_event_minimal(monitor):
    minimal = {
        "id": "1",
        "name": "CI",
        "run_number": 1,
        "head_branch": "",
        "event": "",
        "conclusion": "",
        "html_url": "",
        "head_sha": "",
    }
    event = monitor._to_event(minimal)
    assert "CI" in event.message
    # No head_sha to key on; the run id keeps it distinct.
    assert len(event.fingerprint) == 16


@pytest.mark.asyncio
async def test_seen_ids_are_bounded(monkeypatch):
    """The dedup window evicts oldest ids so it can't grow without bound."""
    from maajun.monitors import base as base_mod

    monkeypatch.setattr(base_mod, "MAX_SEEN_IDS", 3)
    monitor = GitHubActionsMonitor(token="t", repo="owner/name")

    def run(i):
        return {**FAKE_RUN, "id": str(i), "head_sha": f"sha{i}"}

    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"workflow_runs": [run(i) for i in range(5)]}

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)
    await monitor.poll()

    assert len(monitor._seen) == 3
    # Oldest ids evicted, newest kept.
    assert "0" not in monitor._seen
    assert "4" in monitor._seen


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def _run(**overrides):
    run = {
        "id": 1, "name": "CI", "workflow_id": 100, "run_number": 1,
        "head_branch": "main", "event": "push", "conclusion": "failure",
        "html_url": "", "head_sha": "abc12345def67890",
    }
    run.update(overrides)
    return run


def test_two_workflows_failing_on_one_commit_are_two_incidents(monitor):
    """Regression: keyed on head_sha alone, four of five failures vanished.

    A commit that breaks the linter and the tests produced a single incident;
    every other workflow's failure was dropped as a duplicate.
    """
    lint = monitor._to_event(_run(workflow_id=100, name="Lint"))
    tests = monitor._to_event(_run(workflow_id=200, name="Tests"))

    assert lint.fingerprint != tests.fingerprint


def test_the_same_workflow_and_commit_is_one_incident(monitor):
    """A re-run of the same failure must not be reported again."""
    first = monitor._to_event(_run(id=1, run_number=1))
    rerun = monitor._to_event(_run(id=2, run_number=2))

    assert first.fingerprint == rerun.fingerprint


def test_the_same_workflow_on_two_commits_are_two_incidents(monitor):
    a = monitor._to_event(_run(head_sha="aaa111"))
    b = monitor._to_event(_run(head_sha="bbb222"))

    assert a.fingerprint != b.fingerprint


def test_workflow_name_distinguishes_runs_without_a_workflow_id(monitor):
    lint = monitor._to_event(_run(workflow_id=None, name="Lint"))
    tests = monitor._to_event(_run(workflow_id=None, name="Tests"))

    assert lint.fingerprint != tests.fingerprint


def test_the_fingerprint_matches_the_width_used_everywhere_else(monitor):
    assert len(monitor._to_event(_run()).fingerprint) == 16
