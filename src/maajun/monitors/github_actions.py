"""GitHub Actions monitor — polls for failed workflow runs."""

from __future__ import annotations

from typing import Any

import httpx

from maajun.monitors.base import ErrorEvent, HTTPPollMonitor
from maajun.utils import github_headers


class GitHubActionsMonitor(HTTPPollMonitor):
    def __init__(
        self,
        token: str,
        repo: str,
        *,
        burst_threshold: int = 1,
        burst_window_seconds: float = 60.0,
    ):
        """
        Args:
            token: GitHub personal access token with repo scope.
            repo: "owner/name" format.
        """
        self.repo = repo
        super().__init__(
            httpx.AsyncClient(headers=github_headers(token), timeout=30),
            burst_threshold=burst_threshold,
            burst_window_seconds=burst_window_seconds,
        )

    @property
    def name(self) -> str:
        return f"gh-actions:{self.repo}"

    async def _fetch(self) -> list[dict[str, Any]]:
        url = f"https://api.github.com/repos/{self.repo}/actions/runs"
        params = {
            "status": "failure",
            "per_page": 20,
            "sort": "created",
            "direction": "desc",
        }
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("workflow_runs", [])

    def _item_id(self, item: dict[str, Any]) -> str:
        return str(item["id"])

    def _to_event(self, run: dict[str, Any]) -> ErrorEvent:
        name = run.get("name", "unknown workflow")
        head_branch = run.get("head_branch", "")
        run_number = run.get("run_number", 0)
        title = f"Workflow failed: {name} (#{run_number})"

        details_parts = [
            f"Workflow: {name}",
            f"Run: #{run_number}",
            f"Branch: {head_branch}",
            f"Event: {run.get('event', 'unknown')}",
            f"Status: {run.get('conclusion', run.get('status', 'unknown'))}",
        ]

        html_url = run.get("html_url", "")
        if html_url:
            details_parts.append(f"Link: {html_url}")

        commit_sha = run.get("head_sha", "")
        if commit_sha:
            details_parts.append(f"Commit: {commit_sha[:8]}")

        details = "\n".join(details_parts)

        fp = run.get("head_sha") or str(run["id"])

        return ErrorEvent(
            source=self.name,
            message=title[:200],
            details=details,
            fingerprint=fp,
        )
