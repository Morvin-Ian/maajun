from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from maajun.config import Config, RepoConfig
from maajun.vcs import GitHubClient, GitHubError

# Answers "is this source readable here?" as (ok, detail, warn). Injected so
# build_status stays pure: probing shells out to systemctl and docker.
SourceProbe = Callable[[str, str], tuple[bool, str, bool]]


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""
    warn: bool = False
    # Informational lines are shown but do not fail the overall status.
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


def log_file_check(log_path: str, label: str = "", suffix: str = "") -> Check:
    """Existence *and* readability.

    A missing file is only a warning — the app may create it on its first
    error. An unreadable one is a hard failure: it is the classic VPS
    misconfiguration (maajun running as a non-root user against a root-owned
    /var/log file), and the daemon would otherwise log an exception every poll
    while `status` reported everything fine.
    """
    path = Path(log_path).expanduser()
    described = f"{label}log file {log_path}{suffix}"
    if not path.exists():
        return Check(described, False, "not found yet", warn=True, counts=False)
    if not os.access(path, os.R_OK):
        return Check(
            described, False,
            "exists but is not readable — check permissions, or run maajun "
            "as a user that can read it",
        )
    return Check(described, True, counts=False)


def source_check(
    kind: str, target: str, label: str, probe: SourceProbe | None
) -> Check:
    """One runtime source, probed if a prober was supplied."""
    if kind == "file":
        return log_file_check(target, label=label)
    described = f"{label}{kind}: {target}"
    if probe is None:
        return Check(described, True, "(not checked)", warn=True, counts=False)
    ok, detail, warn = probe(kind, target)
    return Check(described, ok, detail, warn=warn, counts=not warn)


def describe_transport(config: Config, has_token: bool) -> str:
    """The transport branches are actually pushed over, "auto" resolved."""
    transport = config.github.transport
    if transport != "auto":
        return transport.upper() if transport == "ssh" else "HTTPS"
    return "HTTPS" if has_token else "SSH"


def credential_label(source: str) -> str:
    """What the GitHub credential is, so a surprise login is visible."""
    if source == "gh":
        return "GitHub credential from the gh CLI"
    return "GitHub token stored"


def build_monitor_checks(
    config: Config, repos: list[RepoConfig], probe: SourceProbe | None
) -> list[Check]:
    """The Monitors section: every error source, and which repo it feeds.

    Runtime sources are reported per repo, because that is how they are
    configured — a repo with none is a repo whose 500s nobody sees, and that
    fails the preflight unless it says `runtime = "none"` on purpose.
    """
    checks: list[Check] = []
    grouped = config.sources_by_repo(repos)
    label_repo = len(repos) > 1
    watches_actions = bool(config.monitor.github_actions_repos)

    if not watches_actions and not any(sources for _, sources in grouped):
        checks.append(Check(
            "At least one monitor configured", False,
            "run 'maajun discover --save', or add monitor.log_files",
        ))

    for repo_config, sources in grouped:
        label = f"{repo_config.repo} — " if (label_repo and repo_config) else ""
        deployment = repo_config.deployment if repo_config else None
        if deployment and deployment.path:
            exists = Path(deployment.path).expanduser().is_dir()
            checks.append(Check(
                f"{label}folder {deployment.path}", exists,
                "" if exists else "not a directory on this host",
                warn=not exists, counts=exists,
            ))
        for kind, target in sources:
            checks.append(source_check(kind, target, label, probe))
        if sources or repo_config is None:
            continue
        if deployment and deployment.runtime == "none":
            checks.append(Check(
                f"{label}no runtime source", True,
                'runtime = "none"', counts=False,
            ))
        else:
            checks.append(Check(
                f"{label}runtime error source", False,
                "none configured — nothing watches this app's failed "
                "requests; run 'maajun discover --save'",
            ))

    if watches_actions:
        checks.append(Check(
            f"GitHub Actions: {', '.join(config.monitor.github_actions_repos)}",
            True, counts=False,
        ))
    return checks


def build_status(
    config: Config,
    *,
    provider: str,
    has_key: bool,
    has_token: bool,
    repos: list[RepoConfig],
    network: tuple[str | None, dict[str, bool]] | None,
    probe: SourceProbe | None = None,
    token_source: str = "",
) -> tuple[list[Section], bool]:
    ai = Section("AI provider", [
        Check(f"API key for {provider}", has_key, "" if has_key else "run 'maajun setup'"),
    ])

    # No repo is a note, not a failure — reports go to disk. Once one is
    # configured, a working token becomes required.
    github = Section("GitHub", [])
    transport = describe_transport(config, has_token)
    if not repos:
        github.checks.append(Check(
            "Repository configured", False,
            "not set — reports go to disk instead of PRs; run 'maajun setup'",
            warn=True, counts=False,
        ))
    else:
        github.checks.append(Check(
            credential_label(token_source), has_token,
            "" if has_token else "run 'gh auth login', or 'maajun setup'",
        ))
        if has_token and transport:
            github.checks.append(Check(
                f"Pushing over {transport}", True, counts=False,
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

    monitors = Section("Monitors", build_monitor_checks(config, repos, probe))

    sections = [ai, github, monitors]
    ok = all(
        check.ok for section in sections for check in section.checks if check.counts
    )
    return sections, ok
