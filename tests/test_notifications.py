"""Tests for the email notification system."""

import pytest

from maajun.config import EmailConfig
from maajun.notifications import Notifier


def email_config(**overrides) -> EmailConfig:
    base = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "username": "bot@example.com",
        "password": "secret",
        "from_addr": "bot@example.com",
        "to_addrs": ["dev@example.com"],
    }
    base.update(overrides)
    return EmailConfig(**base)


@pytest.fixture
def notifier():
    return Notifier(email_config())


def capture_sends(notifier, monkeypatch):
    """Replace the blocking SMTP send with a recorder."""
    sent = []
    monkeypatch.setattr(
        notifier,
        "_send_sync",
        lambda subject, body: sent.append({"subject": subject, "body": body}),
    )
    return sent


def test_enabled_when_configured(notifier):
    assert notifier.enabled


def test_disabled_by_default():
    assert not Notifier().enabled


@pytest.mark.parametrize("missing", ["smtp_host", "from_addr", "to_addrs"])
def test_disabled_when_field_missing(missing):
    value = [] if missing == "to_addrs" else ""
    n = Notifier(email_config(**{missing: value}))
    assert not n.enabled


@pytest.mark.asyncio
async def test_notify_pr_created_sends_email(notifier, monkeypatch):
    sent = capture_sends(notifier, monkeypatch)

    await notifier.notify_pr_created(
        repo="owner/name",
        pr_url="https://github.com/owner/name/pull/1",
        pr_title="[maajun] Test error",
        error_message="IndexError: list index out of range",
        mode="fix",
        fingerprint="abc123",
    )

    assert len(sent) == 1
    assert sent[0]["subject"] == "[maajun] Test error"
    body = sent[0]["body"]
    assert "owner/name" in body
    assert "https://github.com/owner/name/pull/1" in body
    assert "abc123" in body
    assert "fix" in body


@pytest.mark.asyncio
async def test_notify_incident_failed_sends_email(notifier, monkeypatch):
    sent = capture_sends(notifier, monkeypatch)

    await notifier.notify_incident_failed(
        repo="owner/name",
        error_message="ConnectionError",
        fingerprint="def456",
        reason="GitHub API timeout",
    )

    assert len(sent) == 1
    assert "incident failed" in sent[0]["subject"]
    body = sent[0]["body"]
    assert "def456" in body
    assert "GitHub API timeout" in body


@pytest.mark.asyncio
async def test_no_op_when_disabled(monkeypatch):
    n = Notifier()

    def boom(subject, body):
        raise AssertionError("should not be called")

    monkeypatch.setattr(n, "_send_sync", boom)

    await n.notify_pr_created(
        repo="x", pr_url="x", pr_title="x", error_message="x", mode="x", fingerprint="x",
    )
    await n.notify_incident_failed(repo="x", error_message="x", fingerprint="x", reason="x")


@pytest.mark.asyncio
async def test_smtp_failure_does_not_raise(notifier, monkeypatch):
    def boom(subject, body):
        raise Exception("connection refused")

    monkeypatch.setattr(notifier, "_send_sync", boom)

    # Should not raise
    await notifier.notify_pr_created(
        repo="x", pr_url="x", pr_title="x", error_message="x", mode="x", fingerprint="x",
    )


def test_send_sync_builds_message(notifier, monkeypatch):
    """The blocking sender logs in, sends one message, and uses STARTTLS on 587."""
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, msg):
            calls.append(("send", msg))

    monkeypatch.setattr("maajun.notifications.smtplib.SMTP", FakeSMTP)

    notifier._send_sync("subject line", "hello body")

    kinds = [c[0] for c in calls]
    assert kinds == ["connect", "starttls", "login", "send", "quit"]
    assert ("login", "bot@example.com", "secret") in calls
    msg = next(c[1] for c in calls if c[0] == "send")
    assert msg["Subject"] == "subject line"
    assert msg["From"] == "bot@example.com"
    assert msg["To"] == "dev@example.com"


def test_password_falls_back_to_env(monkeypatch):
    n = Notifier(email_config(password=""))
    monkeypatch.setenv("MAAJUN_SMTP_PASSWORD", "from-env")
    logins = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            logins.append(password)

        def send_message(self, msg):
            pass

    monkeypatch.setattr("maajun.notifications.smtplib.SMTP", FakeSMTP)

    n._send_sync("s", "b")
    assert logins == ["from-env"]


def test_port_465_uses_implicit_tls(monkeypatch):
    n = Notifier(email_config(smtp_port=465))
    used = []

    class FakeSMTPSSL:
        def __init__(self, host, port, timeout=None):
            used.append(("ssl", port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            used.append(("send",))

    monkeypatch.setattr("maajun.notifications.smtplib.SMTP_SSL", FakeSMTPSSL)

    n._send_sync("s", "b")
    assert ("ssl", 465) in used
    assert ("send",) in used


@pytest.mark.asyncio
async def test_close(notifier):
    await notifier.close()
