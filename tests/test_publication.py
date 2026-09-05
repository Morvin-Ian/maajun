import pytest

from maajun.publication import choose_runtime_artifact_target
from maajun.vcs import GitHubError


class GitHub:
    def __init__(self, visibilities=None, failures=None):
        self.visibilities = visibilities or {}
        self.failures = failures or {}

    async def repository_visibility(self, repo):
        if repo in self.failures:
            raise GitHubError(self.failures[repo])
        return self.visibilities[repo]


@pytest.mark.parametrize("visibility", ["private", "internal"])
async def test_non_public_runtime_target_is_allowed(visibility):
    decision = await choose_runtime_artifact_target(
        GitHub({"owner/app": visibility}), "owner/app"
    )

    assert decision.repository == "owner/app"
    assert decision.visibility == visibility


async def test_public_runtime_target_requires_explicit_opt_in():
    github = GitHub({"owner/app": "public"})

    denied = await choose_runtime_artifact_target(github, "owner/app")
    allowed = await choose_runtime_artifact_target(
        github, "owner/app", allow_public=True
    )

    assert not denied.allowed
    assert "is public" in denied.reason
    assert allowed.repository == "owner/app"


async def test_public_runtime_artifact_can_use_a_private_fallback():
    decision = await choose_runtime_artifact_target(
        GitHub({"owner/app": "public", "owner/incidents": "private"}),
        "owner/app",
        fallback_repo="owner/incidents",
    )

    assert decision.repository == "owner/incidents"
    assert "routed" in decision.reason


async def test_public_fallback_is_rejected():
    decision = await choose_runtime_artifact_target(
        GitHub({"owner/app": "public", "owner/incidents": "public"}),
        "owner/app",
        fallback_repo="owner/incidents",
    )

    assert not decision.allowed
    assert "also public" in decision.reason


async def test_unknown_visibility_fails_closed():
    decision = await choose_runtime_artifact_target(
        GitHub(failures={"owner/app": "API unavailable"}), "owner/app"
    )

    assert not decision.allowed
    assert "could not be verified" in decision.reason
