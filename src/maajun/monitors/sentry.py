"""Sentry monitor — polls the Sentry API for unresolved issues."""

from __future__ import annotations

from typing import Any

import httpx

from maajun.monitors.base import ErrorEvent, HTTPPollMonitor


class SentryMonitor(HTTPPollMonitor):
    def __init__(
        self,
        auth_token: str,
        org_slug: str,
        project_slug: str,
        *,
        base_url: str = "https://sentry.io/api/0",
        poll_interval: float | None = None,
    ):
        self.org_slug = org_slug
        self.project_slug = project_slug
        self.base_url = base_url.rstrip("/")
        super().__init__(
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {auth_token}"},
                timeout=30,
            )
        )

    @property
    def name(self) -> str:
        return f"sentry:{self.org_slug}/{self.project_slug}"

    async def _fetch(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/projects/{self.org_slug}/{self.project_slug}/issues/"
        params = {
            "query": "is:unresolved",
            "sort": "date",
            "limit": 20,
        }
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _item_id(self, item: dict[str, Any]) -> str:
        return str(item["id"])

    def _to_event(self, issue: dict[str, Any]) -> ErrorEvent:
        title = issue.get("title", "Unknown Sentry issue")
        culprit = issue.get("culprit", "")
        metadata = issue.get("metadata", {})
        value = metadata.get("value", "")
        type_ = metadata.get("type", "")

        details_parts = [f"Type: {type_}" if type_ else ""]
        if value:
            details_parts.append(f"Value: {value}")
        if culprit:
            details_parts.append(f"Culprit: {culprit}")
        platform = issue.get("platform", "")
        if platform:
            details_parts.append(f"Platform: {platform}")
        count = issue.get("count", 0)
        if count:
            details_parts.append(f"Events: {count}")
        user_count = issue.get("userCount", 0)
        if user_count:
            details_parts.append(f"Users affected: {user_count}")

        permalink = issue.get("permalink", "")
        if permalink:
            details_parts.append(f"Link: {permalink}")

        details = "\n".join(p for p in details_parts if p)

        return ErrorEvent(
            source=self.name,
            message=title[:200],
            details=details,
            fingerprint=issue.get("shortId", str(issue["id"])),
        )
