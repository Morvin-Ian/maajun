"""Minimal GitHub REST client for token validation and PR creation."""

from __future__ import annotations

from typing import Any

import httpx

from maajun.utils import github_headers

API_URL = "https://api.github.com"


class GitHubError(Exception):
    """A GitHub API call failed. Message is safe to show the user."""


class GitHubClient:
    def __init__(self, token: str, *, api_url: str = API_URL,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_url = api_url.rstrip("/")
        self._headers = github_headers(token)
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_url,
            headers=self._headers,
            timeout=30,
            transport=self._transport,
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GitHubError(f"Could not reach GitHub: {e}") from e

    async def validate_token(self) -> str:
        """Return the authenticated login, or raise GitHubError."""
        resp = await self._request("GET", "/user")
        if resp.status_code == 401:
            raise GitHubError("GitHub rejected the token. Check it and try again.")
        if resp.status_code != 200:
            raise GitHubError(f"GitHub /user returned {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("login", "")

    async def can_push(self, repo: str) -> bool:
        """Whether the token has push (contents write) access to the repo."""
        resp = await self._request("GET", f"/repos/{repo}")
        if resp.status_code == 404:
            raise GitHubError(
                f"Repo '{repo}' not found or the token has no access to it."
            )
        if resp.status_code != 200:
            raise GitHubError(f"GitHub /repos returned {resp.status_code}: {resp.text[:200]}")
        return bool(resp.json().get("permissions", {}).get("push"))

    async def create_pull_request(
        self, repo: str, *, head: str, base: str, title: str, body: str,
    ) -> str:
        """Open a PR and return its URL; reuses an existing PR for the branch."""
        resp = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        if resp.status_code == 201:
            return resp.json()["html_url"]
        if resp.status_code == 422:
            existing = await self.find_pull_request(repo, head=head)
            if existing:
                return existing
        raise GitHubError(
            f"Could not create PR ({resp.status_code}): {self._error_detail(resp)}"
        )

    async def find_pull_request(self, repo: str, *, head: str) -> str | None:
        owner = repo.split("/")[0]
        resp = await self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        if resp.status_code != 200:
            return None
        pulls = resp.json()
        return pulls[0]["html_url"] if pulls else None

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            return resp.text[:200]
        errors = "; ".join(
            e.get("message", str(e)) if isinstance(e, dict) else str(e)
            for e in data.get("errors", [])
        )
        message = data.get("message", "")
        return f"{message} {errors}".strip() or resp.text[:200]
