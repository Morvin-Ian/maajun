"""Notifications — sends plain-text email alerts via SMTP.

Delivery failures are logged and never interrupt the incident pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage

from maajun.config import EmailConfig

log = logging.getLogger(__name__)


class Notifier:
    """Sends notification emails using the configured SMTP server."""

    def __init__(self, email: EmailConfig | None = None):
        self.email = email or EmailConfig()

    @property
    def enabled(self) -> bool:
        e = self.email
        return bool(e.smtp_host and e.from_addr and e.to_addrs)

    async def notify_pr_created(
        self,
        *,
        repo: str,
        pr_url: str,
        pr_title: str,
        error_message: str,
        mode: str,
        fingerprint: str,
    ) -> None:
        body = (
            f"Maajun opened a pull request.\n\n"
            f"Repo: {repo}\n"
            f"Error: {error_message}\n"
            f"PR: {pr_url}\n"
            f"Mode: {mode}\n"
            f"Fingerprint: {fingerprint}\n"
        )
        await self._send(pr_title, body)

    async def notify_incident_failed(
        self,
        *,
        repo: str,
        error_message: str,
        fingerprint: str,
        reason: str,
    ) -> None:
        body = (
            f"Maajun failed to process an incident.\n\n"
            f"Repo: {repo}\n"
            f"Error: {error_message}\n"
            f"Reason: {reason}\n"
            f"Fingerprint: {fingerprint}\n"
        )
        await self._send(f"[maajun] incident failed: {error_message[:80]}", body)

    async def _send(self, subject: str, body: str) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(self._send_sync, subject, body)
        except Exception:
            log.exception("email notification failed (subject: %s)", subject)

    def _send_sync(self, subject: str, body: str) -> None:
        e = self.email
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = e.from_addr
        msg["To"] = ", ".join(e.to_addrs)
        msg.set_content(body)

        password = e.password or os.environ.get("MAAJUN_SMTP_PASSWORD", "")

        if e.smtp_port == 465:
            server = smtplib.SMTP_SSL(e.smtp_host, e.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=15)
        with server:
            if e.smtp_port != 465:
                server.starttls()
            if e.username:
                server.login(e.username, password)
            server.send_message(msg)

    async def close(self) -> None:
        """Kept for interface compatibility; SMTP connections are per-send."""
