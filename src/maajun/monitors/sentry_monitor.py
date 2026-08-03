"""Sentry monitor — polls for unresolved issues via the Sentry API."""

from __future__ import annotations

from typing import Any

import httpx

from maajun.monitors.base import ErrorEvent, HTTPPollMonitor
from maajun.monitors.registry import MonitorRegistry

DEFAULT_BASE_URL = "https://sentry.io"


@MonitorRegistry.register("sentry")
class SentryMonitor(HTTPPollMonitor):
    def __init__(
        self,
        token: str,
        org: str,
        project: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        stats_period: str = "24h",
        query: str = "is:unresolved",
        burst_threshold: int = 1,
        burst_window_seconds: float = 60.0,
    ):
        """
        Args:
            token: Sentry auth token with project:read scope.
            org: Sentry organization slug.
            project: Sentry project slug.
            base_url: override for self-hosted Sentry.
        """
        self.org = org
        self.project = project
        self.base_url = base_url.rstrip("/")
        self.stats_period = stats_period
        self.query = query
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        super().__init__(
            httpx.AsyncClient(headers=headers, timeout=30),
            burst_threshold=burst_threshold,
            burst_window_seconds=burst_window_seconds,
        )

    @property
    def name(self) -> str:
        return f"sentry:{self.org}/{self.project}"

    async def _fetch(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/0/projects/{self.org}/{self.project}/issues/"
        params = {
            "statsPeriod": self.stats_period,
            "query": self.query,
            "limit": 100,
        }
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _item_id(self, item: dict[str, Any]) -> str:
        return str(item["id"])

    def _to_event(self, issue: dict[str, Any]) -> ErrorEvent:
        title = issue.get("title", "Unknown issue")
        culprit = issue.get("culprit", "")
        level = issue.get("level", "error")
        count = issue.get("count", 0)
        first_seen = issue.get("firstSeen", "")
        last_seen = issue.get("lastSeen", "")
        permalink = issue.get("permalink", "")

        detail_lines = [
            f"Issue: {title}",
            f"Culprit: {culprit}",
            f"Level: {level}",
            f"Events: {count}",
            f"First seen: {first_seen}",
            f"Last seen: {last_seen}",
        ]
        if permalink:
            detail_lines.append(f"Link: {permalink}")

        return ErrorEvent(
            source=self.name,
            message=f"Sentry: {title}"[:200],
            details="\n".join(detail_lines),
            fingerprint=str(issue["id"]),
        )
