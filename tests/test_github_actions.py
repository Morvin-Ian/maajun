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
    assert "abc12345def67890" in event.fingerprint
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
    assert event.fingerprint == "1"
