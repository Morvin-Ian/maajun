from __future__ import annotations

import hashlib
from typing import Any

import httpx

from maajun.monitors.base import FINGERPRINT_LENGTH, ErrorEvent, HTTPPollMonitor
from maajun.vcs.api import github_headers


class GitHubActionsMonitor(HTTPPollMonitor):
    def __init__(
        self,
        token: str,
        repo: str,
        *,
        burst_threshold: int = 1,
        burst_window_seconds: float = 60.0,
    ):
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

        return ErrorEvent(
            source=self.name,
            message=title[:200],
            details=details,
            fingerprint=self._fingerprint(run),
        )

    @staticmethod
    def _fingerprint(run: dict[str, Any]) -> str:
        """Identify the failure by workflow *and* commit, not commit alone.

        Keyed on head_sha by itself, one commit that broke five workflows
        produced a single incident and the other four were dropped as
        duplicates. The workflow is what distinguishes "the linter failed"
        from "the tests failed" on the same commit.

        Still keyed on the commit rather than the run id, so a re-run of the
        same failure is not reported twice. Hashed to 16 chars like every
        other fingerprint — a raw 40-char sha sat oddly in the same column.

        Hashed directly rather than through monitors.base.fingerprint(): that
        strips digits so a traceback matches across differing line numbers,
        which would erase the difference between workflow 100 and workflow
        200. This key is already exact, so it needs no normalizing.
        """
        workflow = str(
            run.get("workflow_id") or run.get("name") or "unknown workflow"
        )
        commit = run.get("head_sha") or str(run.get("id", ""))
        key = f"gh-actions\x00{workflow}\x00{commit}"
        return hashlib.sha256(key.encode()).hexdigest()[:FINGERPRINT_LENGTH]
