"""Fail-closed repository selection for passive runtime artifacts."""

from __future__ import annotations

from dataclasses import dataclass

from maajun.vcs import GitHubError

NON_PUBLIC_VISIBILITIES = frozenset({"private", "internal"})


@dataclass(frozen=True)
class PublicationDecision:
    repository: str = ""
    visibility: str = "unknown"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return bool(self.repository)


async def choose_runtime_artifact_target(
    github,
    primary_repo: str,
    *,
    allow_public: bool = False,
    fallback_repo: str = "",
) -> PublicationDecision:
    """Choose a repository without exposing passive evidence by default.

    Public repositories require an explicit opt-in. An inaccessible target or
    a response without visibility is treated as unknown rather than guessed
    private. A configured fallback must itself be non-public.
    """
    visibility, failure = await _visibility(github, primary_repo)
    if visibility in NON_PUBLIC_VISIBILITIES:
        return PublicationDecision(primary_repo, visibility)
    if visibility == "public" and allow_public:
        return PublicationDecision(primary_repo, visibility)

    if visibility == "public":
        reason = (
            f"{primary_repo} is public and public runtime artifacts are not "
            "enabled for this repository"
        )
    else:
        reason = (
            f"GitHub visibility for {primary_repo} could not be verified"
            + (f": {failure}" if failure else "")
        )

    if fallback_repo and fallback_repo != primary_repo:
        fallback_visibility, fallback_failure = await _visibility(
            github, fallback_repo
        )
        if fallback_visibility in NON_PUBLIC_VISIBILITIES:
            return PublicationDecision(
                fallback_repo,
                fallback_visibility,
                f"{reason}; routed to the configured non-public runtime repository",
            )
        if fallback_visibility == "public":
            reason += f"; configured fallback {fallback_repo} is also public"
        else:
            reason += (
                f"; visibility for configured fallback {fallback_repo} could "
                "not be verified"
                + (f": {fallback_failure}" if fallback_failure else "")
            )

    return PublicationDecision(reason=reason)


async def _visibility(github, repo: str) -> tuple[str, str]:
    try:
        visibility = await github.repository_visibility(repo)
    except GitHubError as error:
        return "unknown", str(error)
    if visibility not in {"public", *NON_PUBLIC_VISIBILITIES}:
        return "unknown", f"GitHub returned {visibility!r}"
    return visibility, ""
