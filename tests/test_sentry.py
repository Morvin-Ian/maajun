"""Tests for the Sentry monitor."""

import httpx
import pytest

from maajun.monitors.sentry import SentryMonitor

FAKE_ISSUE = {
    "id": "12345",
    "shortId": "PROJ-1A2B",
    "title": "TypeError: unsupported operand",
    "culprit": "app/views.py in process_order",
    "metadata": {
        "type": "TypeError",
        "value": "unsupported operand type(s) for +: 'int' and 'NoneType'",
    },
    "platform": "python",
    "count": 42,
    "userCount": 7,
    "permalink": "https://sentry.io/organizations/myorg/issues/12345/",
}


@pytest.fixture
def monitor():
    return SentryMonitor(
        auth_token="sntrys_test",
        org_slug="myorg",
        project_slug="myproject",
    )


def test_name(monitor):
    assert monitor.name == "sentry:myorg/myproject"


@pytest.mark.asyncio
async def test_poll_returns_events(monitor, monkeypatch):
    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [FAKE_ISSUE]

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert len(events) == 1
    event = events[0]
    assert event.source == "sentry:myorg/myproject"
    assert "TypeError" in event.message
    assert "PROJ-1A2B" in event.fingerprint
    assert "TypeError" in event.details
    assert "42" in event.details  # count
    assert "7" in event.details  # userCount


@pytest.mark.asyncio
async def test_poll_deduplicates(monitor, monkeypatch):
    call_count = 0

    async def mock_get(url, params=None):
        nonlocal call_count
        call_count += 1

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [FAKE_ISSUE]

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
                return []

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert events == []


@pytest.mark.asyncio
async def test_poll_api_error_returns_empty(monitor, monkeypatch):
    async def mock_get(url, params=None):
        raise httpx.HTTPStatusError("401", request=None, response=httpx.Response(401))

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert events == []


@pytest.mark.asyncio
async def test_poll_multiple_issues(monitor, monkeypatch):
    issues = [
        {**FAKE_ISSUE, "id": "1", "shortId": "PROJ-001", "title": "Error A"},
        {**FAKE_ISSUE, "id": "2", "shortId": "PROJ-002", "title": "Error B"},
        {**FAKE_ISSUE, "id": "3", "shortId": "PROJ-003", "title": "Error C"},
    ]

    async def mock_get(url, params=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return issues

        return FakeResp()

    monkeypatch.setattr(monitor._client, "get", mock_get)

    events = await monitor.poll()
    assert len(events) == 3
    titles = {e.message for e in events}
    assert titles == {"Error A", "Error B", "Error C"}


def test_issue_to_event_minimal(monitor):
    minimal_issue = {
        "id": "99",
        "shortId": "PROJ-99",
        "title": "Something broke",
        "culprit": "",
        "metadata": {"type": "", "value": ""},
        "platform": "",
        "count": 0,
        "userCount": 0,
        "permalink": "",
    }
    event = monitor._to_event(minimal_issue)
    assert event.message == "Something broke"
    assert event.fingerprint == "PROJ-99"
