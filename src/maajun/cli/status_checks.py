from __future__ import annotations

import os
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
        for repo_config in repos:
            try:
                pushable[repo_config.repo] = await client.can_push(repo_config.repo)
            except GitHubError:
                pushable[repo_config.repo] = False
        return login, pushable
    finally:
        await client.aclose()


def _log_file_check(log_path: str) -> Check:
    """Existence *and* readability.

    A missing file is only a warning — the app may create it on its first
    error. An unreadable one is a hard failure: it is the classic VPS
    misconfiguration (maajun running as a non-root user against a root-owned
    /var/log file), and the daemon would otherwise log an exception every poll
    while `status` reported everything fine.
    """
    path = Path(log_path).expanduser()
    if not path.exists():
        return Check(
            f"Log file {log_path}", False, "not found yet",
            warn=True, counts=False,
        )
    if not os.access(path, os.R_OK):
        return Check(
            f"Log file {log_path}", False,
            "exists but is not readable — check permissions, or run maajun "
            "as a user that can read it",
        )
    return Check(f"Log file {log_path}", True, counts=False)


def build_status(
    config: Config,
    *,
    provider: str,
    has_key: bool,
    has_token: bool,
    repos: list[RepoConfig],
    network: tuple[str | None, dict[str, bool]] | None,
) -> tuple[list[Section], bool]:
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
        # "stored" is exact again now the keyring is the only source.
        github.checks.append(Check(
            "GitHub token stored", has_token,
            "" if has_token else "run 'maajun setup' to store one",
        ))
        if network is None:
            for repo_config in repos:
                github.checks.append(Check(
                    f"Repository {repo_config.repo}", True,
                    "(reachability not checked)", warn=True, counts=False,
                ))
        else:
            login, pushable = network
            if login is None:
                github.checks.append(Check("Token valid", False, "authentication failed"))
            else:
                github.checks.append(Check(f"Authenticated as {login}", True, counts=False))
                for repo_config in repos:
                    can_push = pushable.get(repo_config.repo, False)
                    github.checks.append(Check(
                        f"Can push to {repo_config.repo}", can_push,
                        "" if can_push else "check token repo access / Contents perm",
                    ))

    monitors = Section("Monitors", [])
    log_paths = list(config.monitor.log_files)
    for repo_config in repos:
        log_paths.extend(repo_config.log_files)
    watches_actions = bool(config.monitor.github_actions_repos)
    if not log_paths and not watches_actions:
        monitors.checks.append(Check(
            "At least one monitor configured", False,
            "add monitor.log_files or GitHub Actions",
        ))
    for log_path in log_paths:
        monitors.checks.append(_log_file_check(log_path))
    if watches_actions:
        monitors.checks.append(Check(
            f"GitHub Actions: {', '.join(config.monitor.github_actions_repos)}",
            True, counts=False,
        ))

    sections = [ai, github, monitors]
    ok = all(
        check.ok for section in sections for check in section.checks if check.counts
    )
    return sections, ok
