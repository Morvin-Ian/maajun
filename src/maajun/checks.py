"""Preflight status checks for `maajun status`.

Pure assembly of the check list (plus the one network probe) lives here so it
can be unit-tested without a CliRunner; cli.py owns the console and rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maajun.config import Config, RepoConfig
from maajun.vcs import GitHubClient, GitHubError


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""
    warn: bool = False
    # Whether a failure should fail the overall status. Informational lines
    # (missing log files, "reachability not checked") are shown but don't fail.
    counts: bool = True


@dataclass
class Section:
    title: str
    checks: list[Check]


async def gather_github(
    client: GitHubClient, repos: list[RepoConfig]
) -> tuple[str | None, dict[str, bool]]:
    """Probe the token and per-repo push access. Returns (login, pushable).

    login is None if authentication fails; pushable maps repo -> bool.
    """
    try:
        try:
            login = await client.validate_token()
        except GitHubError:
            return None, {}
        pushable = {}
        for rc in repos:
            try:
                pushable[rc.repo] = await client.can_push(rc.repo)
            except GitHubError:
                pushable[rc.repo] = False
        return login, pushable
    finally:
        await client.aclose()


def build_status(
    config: Config,
    *,
    provider: str,
    has_key: bool,
    has_token: bool,
    repos: list[RepoConfig],
    network: tuple[str | None, dict[str, bool]] | None,
) -> tuple[list[Section], bool]:
    """Assemble the status sections and the overall pass/fail.

    `network` is the gather_github result when a probe ran, or None when it was
    skipped (no token, --no-network, or no repos configured).
    """
    ai = Section("AI provider", [
        Check(f"API key for {provider}", has_key, "" if has_key else "run 'maajun setup'"),
    ])

    # GitHub is optional: without a repo, maajun still analyzes errors and
    # writes reports to disk, so an absent repo is a note rather than a failure.
    # Once a repo *is* configured, a working token becomes required.
    github = Section("GitHub", [])
    if not repos:
        github.checks.append(Check(
            "Repository configured", False,
            "not set — reports go to disk instead of PRs; run 'maajun setup'",
            warn=True, counts=False,
        ))
    else:
        github.checks.append(Check(
            "GitHub token stored", has_token,
            "" if has_token else "run 'maajun setup', export GITHUB_TOKEN, "
                                "or run 'gh auth login'",
        ))
        if network is None:
            for rc in repos:
                github.checks.append(Check(
                    f"Repository {rc.repo}", True,
                    "(reachability not checked)", warn=True, counts=False,
                ))
        else:
            login, pushable = network
            if login is None:
                github.checks.append(Check("Token valid", False, "authentication failed"))
            else:
                github.checks.append(Check(f"Authenticated as {login}", True, counts=False))
                for rc in repos:
                    pushes = pushable.get(rc.repo, False)
                    github.checks.append(Check(
                        f"Can push to {rc.repo}", pushes,
                        "" if pushes else "check token repo access / Contents perm",
                    ))

    monitors = Section("Monitors", [])
    log_paths = list(config.monitor.log_files)
    for rc in repos:
        log_paths.extend(rc.log_files)
    ga = bool(config.monitor.github_actions_token and config.monitor.github_actions_repos)
    if not log_paths and not ga:
        monitors.checks.append(Check(
            "At least one monitor configured", False,
            "add monitor.log_files or GitHub Actions",
        ))
    for lf in log_paths:
        # A missing log file is a warning, not a failure: it may be created
        # once the monitored app first logs an error.
        exists = Path(lf).expanduser().exists()
        monitors.checks.append(Check(
            f"Log file {lf}", exists,
            "" if exists else "not found yet", warn=not exists, counts=False,
        ))
    if ga:
        monitors.checks.append(Check(
            f"GitHub Actions: {', '.join(config.monitor.github_actions_repos)}",
            True, counts=False,
        ))

    sections = [ai, github, monitors]
    ok = all(c.ok for s in sections for c in s.checks if c.counts)
    return sections, ok
