"""Tests for the GitHub client, using httpx.MockTransport."""

import json

import httpx
import pytest

from maajun.vcs import GitHubClient, GitHubError


def make_client(handler):
    return GitHubClient("token", transport=httpx.MockTransport(handler))


async def test_validate_token_returns_login():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(200, json={"login": "morvin"})

    client = make_client(handler)
    assert await client.validate_token() == "morvin"


async def test_validate_token_rejects_bad_token():
    client = make_client(lambda request: httpx.Response(401, json={}))
    with pytest.raises(GitHubError, match="rejected"):
        await client.validate_token()


async def test_can_push_reads_permissions():
    def handler(request):
        assert request.url.path == "/repos/owner/name"
        return httpx.Response(200, json={"permissions": {"push": True}})

    client = make_client(handler)
    assert await client.can_push("owner/name") is True


async def test_can_push_missing_repo_raises():
    client = make_client(lambda request: httpx.Response(404, json={}))
    with pytest.raises(GitHubError, match="not found"):
        await client.can_push("owner/name")


async def test_create_pull_request():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/repos/owner/name/pulls"
        payload = json.loads(request.content)
        assert payload["head"] == "maajun/incident-abc"
        assert payload["base"] == "main"
        return httpx.Response(201, json={"html_url": "https://github.com/owner/name/pull/7"})

    client = make_client(handler)
    url = await client.create_pull_request(
        "owner/name", head="maajun/incident-abc", base="main", title="t", body="b"
    )
    assert url == "https://github.com/owner/name/pull/7"


async def test_create_pull_request_reuses_existing_on_422():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                422, json={"errors": [{"message": "A pull request already exists"}]}
            )
        assert request.url.params["head"] == "owner:maajun/incident-abc"
        return httpx.Response(200, json=[{"html_url": "https://github.com/owner/name/pull/3"}])

    client = make_client(handler)
    url = await client.create_pull_request(
        "owner/name", head="maajun/incident-abc", base="main", title="t", body="b"
    )
    assert url == "https://github.com/owner/name/pull/3"


async def test_create_pull_request_error_surfaces_detail():
    client = make_client(
        lambda request: httpx.Response(403, json={"message": "Resource not accessible"})
    )
    with pytest.raises(GitHubError, match="Resource not accessible"):
        await client.create_pull_request(
            "owner/name", head="h", base="main", title="t", body="b"
        )
