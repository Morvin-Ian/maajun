"""Tests for the notification system."""

import pytest

from maajun.notifications import Notifier


@pytest.fixture
def notifier():
    return Notifier(webhook_urls=["https://hooks.slack.com/test"])


def test_enabled_when_urls_present():
    n = Notifier(webhook_urls=["https://hooks.slack.com/test"])
    assert n.enabled


def test_disabled_when_no_urls():
    n = Notifier()
    assert not n.enabled


def test_disabled_when_empty_list():
    n = Notifier(webhook_urls=[])
    assert not n.enabled


@pytest.mark.asyncio
async def test_notify_pr_created_sends_slack(notifier, monkeypatch):
    sent = []

    async def mock_post(url, json=None):
        sent.append({"url": url, "json": json})

        class FakeResp:
            def raise_for_status(self):
                pass

        return FakeResp()

    monkeypatch.setattr(notifier._client, "post", mock_post)

    await notifier.notify_pr_created(
        repo="owner/name",
        pr_url="https://github.com/owner/name/pull/1",
        pr_title="[maajun] Test error",
        error_message="IndexError: list index out of range",
        mode="fix",
        fingerprint="abc123",
    )

    assert len(sent) == 1
    body = sent[0]["json"]
    assert "New PR created" in body["text"]
    assert "owner/name" in body["text"]
    assert "abc123" in body["text"]
    assert "fix" in body["text"]


@pytest.mark.asyncio
async def test_notify_incident_failed_sends_slack(notifier, monkeypatch):
    sent = []

    async def mock_post(url, json=None):
        sent.append({"url": url, "json": json})

        class FakeResp:
            def raise_for_status(self):
                pass

        return FakeResp()

    monkeypatch.setattr(notifier._client, "post", mock_post)

    await notifier.notify_incident_failed(
        repo="owner/name",
        error_message="ConnectionError",
        fingerprint="def456",
        reason="GitHub API timeout",
    )

    assert len(sent) == 1
    body = sent[0]["json"]
    assert "failed to process" in body["text"]
    assert "def456" in body["text"]
    assert "GitHub API timeout" in body["text"]


@pytest.mark.asyncio
async def test_no_op_when_disabled(monkeypatch):
    n = Notifier()
    sent = []

    async def mock_post(url, json=None):
        sent.append(True)
        raise AssertionError("should not be called")

    monkeypatch.setattr(n._client, "post", mock_post)

    await n.notify_pr_created(
        repo="x",
        pr_url="x",
        pr_title="x",
        error_message="x",
        mode="x",
        fingerprint="x",
    )
    await n.notify_incident_failed(repo="x", error_message="x", fingerprint="x", reason="x")

    assert sent == []


@pytest.mark.asyncio
async def test_webhook_failure_does_not_raise(notifier, monkeypatch):
    async def mock_post(url, json=None):
        raise Exception("connection refused")

    monkeypatch.setattr(notifier._client, "post", mock_post)

    # Should not raise
    await notifier.notify_pr_created(
        repo="x",
        pr_url="x",
        pr_title="x",
        error_message="x",
        mode="x",
        fingerprint="x",
    )


@pytest.mark.asyncio
async def test_multiple_webhooks(notifier, monkeypatch):
    sent = []
    notifier.webhook_urls.append("https://hooks.slack.com/second")

    async def mock_post(url, json=None):
        sent.append(url)

        class FakeResp:
            def raise_for_status(self):
                pass

        return FakeResp()

    monkeypatch.setattr(notifier._client, "post", mock_post)

    await notifier.notify_pr_created(
        repo="x",
        pr_url="x",
        pr_title="x",
        error_message="x",
        mode="x",
        fingerprint="x",
    )

    assert len(sent) == 2


@pytest.mark.asyncio
async def test_close(notifier):
    await notifier.close()
