import json

import httpx
import pytest

from maajun.vcs import GitHubClient, GitHubError


def make_client(handler):
    return GitHubClient("token", transport=httpx.MockTransport(handler))


async def test_validate_token_returns_login():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(200, json={"login": "morvin", "id": 123})

    client = make_client(handler)
    assert await client.validate_token() == "morvin"


async def test_authenticated_account_has_a_github_noreply_email():
    client = make_client(
        lambda request: httpx.Response(200, json={"login": "morvin", "id": 123})
    )

    account = await client.authenticated_account()

    assert account.noreply_email == "123+morvin@users.noreply.github.com"


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


async def test_repository_visibility_is_refreshed_before_each_publication():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert request.url.path == "/repos/owner/name"
        visibility = "private" if calls == 1 else "public"
        return httpx.Response(200, json={"visibility": visibility})

    client = make_client(handler)

    assert await client.repository_visibility("owner/name") == "private"
    assert await client.repository_visibility("owner/name") == "public"
    assert calls == 2


async def test_repository_visibility_falls_back_to_private_boolean():
    client = make_client(lambda request: httpx.Response(200, json={"private": False}))

    assert await client.repository_visibility("owner/name") == "public"


async def test_repository_visibility_fails_closed_when_github_omits_it():
    client = make_client(lambda request: httpx.Response(200, json={}))

    with pytest.raises(GitHubError, match="did not report visibility"):
        await client.repository_visibility("owner/name")


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


async def test_get_issue_returns_the_issue_content():
    def handler(request):
        assert request.url.path == "/repos/owner/name/issues/12"
        return httpx.Response(200, json={
            "number": 12,
            "title": "Broken checkout",
            "body": "Traceback and analysis",
            "html_url": "https://github.com/owner/name/issues/12",
            "state": "open",
        })

    issue = await make_client(handler).get_issue("owner/name", 12)

    assert issue.number == 12
    assert issue.title == "Broken checkout"
    assert issue.body == "Traceback and analysis"
    assert issue.state == "open"


async def test_get_issue_rejects_a_pull_request():
    client = make_client(lambda request: httpx.Response(200, json={
        "number": 12,
        "pull_request": {"url": "https://api.github.com/pulls/12"},
    }))

    with pytest.raises(GitHubError, match="pull request, not an issue"):
        await client.get_issue("owner/name", 12)


async def test_get_issue_explains_a_missing_issue():
    client = make_client(lambda request: httpx.Response(404, json={}))

    with pytest.raises(GitHubError, match="was not found"):
        await client.get_issue("owner/name", 404)


async def test_client_is_reused_across_requests():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"login": "morvin", "id": 123})

    client = make_client(handler)
    await client.validate_token()
    first = client.client
    await client.validate_token()

    assert client.client is first  # same pooled client, not a fresh one
    assert calls["n"] == 2


async def test_aclose_releases_client():
    client = make_client(
        lambda request: httpx.Response(200, json={"login": "x", "id": 123})
    )
    await client.validate_token()
    assert client.client is not None
    await client.aclose()
    assert client.client is None
