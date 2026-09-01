from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from maajun.vcs.api import github_headers

API_URL = "https://api.github.com"


class GitHubError(Exception):
    """A GitHub API call failed. Message is safe to show the user."""


@dataclass(frozen=True)
class GitHubAccount:
    login: str
    user_id: int

    @property
    def noreply_email(self) -> str:
        return f"{self.user_id}+{self.login}@users.noreply.github.com"


class GitHubClient:
    def __init__(self, token: str, *, api_url: str = API_URL,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_url = api_url.rstrip("/")
        self.headers = github_headers(token)
        self.transport = transport
        self.client: httpx.AsyncClient | None = None

    def get_client(self) -> httpx.AsyncClient:
        """Return a shared client, so repeated calls reuse the connection
        pool and TLS session instead of handshaking anew each request."""
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                base_url=self.api_url,
                headers=self.headers,
                timeout=30,
                transport=self.transport,
            )
        return self.client

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self.get_client().request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GitHubError(f"Could not reach GitHub: {e}") from e

    async def aclose(self) -> None:
        if self.client is not None and not self.client.is_closed:
            await self.client.aclose()
        self.client = None

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def authenticated_account(self) -> GitHubAccount:
        """Return the account a token acts as, or raise GitHubError."""
        resp = await self.request("GET", "/user")
        if resp.status_code == 401:
            raise GitHubError("GitHub rejected the token. Check it and try again.")
        if resp.status_code != 200:
            raise GitHubError(f"GitHub /user returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        login = data.get("login")
        user_id = data.get("id")
        if not isinstance(login, str) or not login or not isinstance(user_id, int):
            raise GitHubError("GitHub /user did not return an account ID and login.")
        return GitHubAccount(login=login, user_id=user_id)

    async def validate_token(self) -> str:
        """Return the authenticated login, or raise GitHubError."""
        return (await self.authenticated_account()).login

    async def can_push(self, repo: str) -> bool:
        """Whether the token has push (contents write) access to the repo."""
        resp = await self.request("GET", f"/repos/{repo}")
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
        resp = await self.request(
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
            f"Could not create PR ({resp.status_code}): {self.error_detail(resp)}"
        )

    async def create_issue(self, repo: str, *, title: str, body: str) -> str:
        """Open an issue and return its URL.

        Suggest mode's artifact: an analysis that changes no code is an issue,
        not a pull request whose diff is empty.
        """
        resp = await self.request(
            "POST", f"/repos/{repo}/issues", json={"title": title, "body": body},
        )
        if resp.status_code == 201:
            return resp.json()["html_url"]
        raise GitHubError(
            f"Could not create issue ({resp.status_code}): {self.error_detail(resp)}"
        )

    async def find_pull_request(self, repo: str, *, head: str) -> str | None:
        owner = repo.split("/")[0]
        resp = await self.request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        if resp.status_code != 200:
            return None
        pulls = resp.json()
        return pulls[0]["html_url"] if pulls else None

    @staticmethod
    def error_detail(resp: httpx.Response) -> str:
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
