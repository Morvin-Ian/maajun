"""Preflight status checks for `maajun status`.

Pure assembly of the check list (plus the one network probe) lives here so it
can be unit-tested without a CliRunner; cli.py owns the console and rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maajun.auth import MONITOR_SECRET_TYPES, AuthManager
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


def gather_monitor_secrets(auth: AuthManager, config: Config) -> dict[str, bool]:
    """Which configured monitor types have an auth token available."""
    return {
        instance.type: auth.get_monitor_secret(instance.type) is not None
        for instance in config.monitor.instances
        if instance.type in MONITOR_SECRET_TYPES
    }


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


def build_status(
    config: Config,
    *,
    provider: str,
    has_key: bool,
    has_token: bool,
    repos: list[RepoConfig],
    network: tuple[str | None, dict[str, bool]] | None,
    monitor_secrets: dict[str, bool] | None = None,
) -> tuple[list[Section], bool]:
    """Assemble the status sections and the overall pass/fail.

    `network` is the gather_github result when a probe ran, or None when it was
    skipped (no token, --no-network, or no repos configured).

    `monitor_secrets` maps a monitor type to whether its auth token was found.
    Passed in rather than looked up here so this stays free of I/O.
    """
    monitor_secrets = monitor_secrets or {}
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
    watches_actions = bool(
        config.monitor.github_actions_token and config.monitor.github_actions_repos
    )
    watches_instances = bool(config.monitor.instances)
    if not log_paths and not watches_actions and not watches_instances:
        monitors.checks.append(Check(
            "At least one monitor configured", False,
            "add monitor.log_files or GitHub Actions",
        ))
    for log_path in log_paths:
        # A missing log file is a warning, not a failure: it may be created
        # once the monitored app first logs an error.
        exists = Path(log_path).expanduser().exists()
        monitors.checks.append(Check(
            f"Log file {log_path}", exists,
            "" if exists else "not found yet", warn=not exists, counts=False,
        ))
    for instance in config.monitor.instances:
        label = instance.type
        details = instance.monitor_kwargs()
        if instance.type == "sentry":
            label = f"sentry: {details.get('org', '?')}/{details.get('project', '?')}"
        # A monitor whose token is missing fails the daemon at startup, so it
        # must fail here too rather than reporting "Ready".
        needs_token = instance.type in monitor_secrets and "token" not in details
        if needs_token and not monitor_secrets[instance.type]:
            monitors.checks.append(Check(
                label, False,
                f"no auth token — set {AuthManager.monitor_env_var(instance.type)} "
                f"or run 'maajun setup --{instance.type} ...'",
            ))
        else:
            monitors.checks.append(Check(label, True, counts=False))
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
