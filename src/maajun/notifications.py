"""Notifications — sends alerts via webhooks (Slack, Discord, etc.)."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    """Sends notifications to configured webhook URLs."""

    def __init__(self, webhook_urls: list[str] | None = None):
        self.webhook_urls = webhook_urls or []
        self._client = httpx.AsyncClient(timeout=15)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_urls)

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
        if not self.enabled:
            return

        mode_emoji = "🔧" if mode == "fix" else "📋"
        text = (
            f"{mode_emoji} *New PR created by Maajun*\n"
            f"*Repo:* `{repo}`\n"
            f"*Error:* {error_message}\n"
            f"*PR:* <{pr_url}|{pr_title}>\n"
            f"*Mode:* {mode} | *Fingerprint:* `{fingerprint}`"
        )
        await self._send_slack(text)

    async def notify_incident_failed(
        self,
        *,
        repo: str,
        error_message: str,
        fingerprint: str,
        reason: str,
    ) -> None:
        if not self.enabled:
            return

        text = (
            f"⚠️ *Maajun failed to process an incident*\n"
            f"*Repo:* `{repo}`\n"
            f"*Error:* {error_message}\n"
            f"*Reason:* {reason}\n"
            f"*Fingerprint:* `{fingerprint}`"
        )
        await self._send_slack(text)

    async def _send_slack(self, text: str) -> None:
        for url in self.webhook_urls:
            try:
                resp = await self._client.post(
                    url,
                    json={"text": text, "unfurl_links": False},
                )
                resp.raise_for_status()
            except Exception:
                log.exception("slack webhook failed: %s", url)

    async def close(self) -> None:
        await self._client.aclose()
