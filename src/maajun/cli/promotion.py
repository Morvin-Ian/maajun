from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.cli.shared import app, console, load_config
from maajun.config import Config, RepoConfig
from maajun.daemon import build_daemon_for_report
from maajun.daemon.store import ARTIFACT_ISSUE, ARTIFACT_REPORT, IncidentStore
from maajun.monitors import fingerprint
from maajun.progress import working
from maajun.utils import truncate
from maajun.vcs import GitHubError, GitHubIssue

ISSUE_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>\d+)/?$"
)


@dataclass(frozen=True)
class PromotionTarget:
    fingerprint: str
    repo: str
    issue_url: str
    issue_number: int


def issue_number(url: str) -> int | None:
    match = ISSUE_URL_RE.match(url or "")
    return int(match.group("number")) if match else None


def promotion_candidates(store: IncidentStore, repo: str | None = None) -> list[dict]:
    return [
        row
        for row in store.all(repo)
        if row["artifact_kind"] == ARTIFACT_ISSUE and issue_number(row["pr_url"])
    ]


def resolve_promotion(
    store: IncidentStore, identifier: str, repo: str | None = None
) -> PromotionTarget:
    """Resolve a recorded Maajun issue by URL, number, or fingerprint prefix."""
    candidates = promotion_candidates(store, repo)
    url_match = ISSUE_URL_RE.match(identifier.rstrip("/"))
    if url_match:
        if repo and url_match.group("repo") != repo:
            raise ValueError(
                f"Issue URL belongs to {url_match.group('repo')}, not requested repo {repo}."
            )
        matches = [row for row in candidates if row["pr_url"].rstrip("/") == identifier.rstrip("/")]
    elif identifier.isdigit():
        if repo is None and len({row["repo"] for row in candidates}) > 1:
            raise ValueError("An issue number needs --repo when multiple repos have incidents.")
        matches = [row for row in candidates if issue_number(row["pr_url"]) == int(identifier)]
    else:
        matches = [row for row in candidates if row["fingerprint"].startswith(identifier)]

    if not matches:
        raise ValueError(
            f"No recorded Maajun issue matches {identifier!r}. Run 'maajun incidents' to list them."
        )
    if len(matches) > 1:
        choices = ", ".join(f"{row['fingerprint']} ({row['repo']})" for row in matches)
        raise ValueError(
            f"{identifier!r} is ambiguous: {choices}. "
            "Pass --repo or more fingerprint characters."
        )
    row = matches[0]
    number = issue_number(row["pr_url"])
    assert number is not None
    return PromotionTarget(row["fingerprint"], row["repo"], row["pr_url"], number)


def configured_repo(config: Config, name: str) -> RepoConfig:
    match = next((entry for entry in config.github.repos if entry.repo == name), None)
    if match is None:
        raise ValueError(
            f"{name} is not configured on this machine. Add it with 'maajun add-repo {name}'."
        )
    return match


def validate_issue(issue: GitHubIssue, target: PromotionTarget) -> None:
    """Ensure GitHub still exposes the exact recorded, open issue."""
    if issue.html_url.rstrip("/") != target.issue_url.rstrip("/"):
        raise GitHubError("GitHub returned a different issue than the recorded artifact.")
    if issue.state != "open":
        raise GitHubError(
            f"Issue {target.repo}#{target.issue_number} is {issue.state}; "
            "reopen it before promotion."
        )


@app.command()
def promote(
    incident: str = typer.Argument(
        help="Recorded fingerprint, GitHub issue URL, or issue number to turn into a fix"
    ),
    repo: str | None = typer.Option(
        None, "--repo", "-r", help="Repository for an issue number or ambiguous fingerprint"
    ),
    base_branch: str | None = typer.Option(
        None, "--base-branch", "-b", help="Branch to base the fix on"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Analyze without opening a PR"),
    verbose: bool = typer.Option(False, "--verbose", help="Show debug output"),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Turn a recorded Maajun issue into a fix PR for owner review."""
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    config = load_config(config_path)
    if not config.github.repos:
        console.print("[red]✗ Promotion needs a configured GitHub repository.[/red]")
        raise typer.Exit(1)

    daemon = None
    try:
        daemon = build_daemon_for_report(config)
        target = resolve_promotion(daemon.store, incident, repo)
        saved_repo = configured_repo(config, target.repo)
        run_repo = saved_repo.model_copy(deep=True)
        run_repo.mode = "fix"
        if base_branch:
            run_repo.base_branch = base_branch
    except (RuntimeError, ValueError) as exc:
        if daemon is not None:
            asyncio.run(daemon.aclose())
            daemon.store.close()
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc

    existing = daemon.store.get(
        fingerprint(f"promotion:{target.repo}#{target.issue_number}"), target.repo
    )
    if existing and existing["artifact_kind"] == "pr" and existing["pr_url"]:
        asyncio.run(daemon.aclose())
        daemon.store.close()
        console.print(f"[green]✓ Fix PR already exists:[/green] {existing['pr_url']}")
        return

    async def run(progress):
        try:
            issue = await daemon.github.get_issue(target.repo, target.issue_number)
            validate_issue(issue, target)
            return await daemon.handle_promotion(
                issue,
                target.fingerprint,
                run_repo,
                dry_run=dry_run,
                progress=progress,
            )
        finally:
            await daemon.aclose()
            daemon.store.close()

    console.print(Panel(
        "[bold]Maajun promotion[/bold]\n\n"
        f"Issue: {target.issue_url}\n"
        f"Repo:  {target.repo} (base: {run_repo.base_branch})\n"
        "Mode:  fix for this run only"
        + ("\n[yellow]Dry run — no branch or PR will be created[/yellow]" if dry_run else ""),
        border_style="blue",
    ))
    try:
        if dry_run or verbose:
            result = asyncio.run(run(lambda phase: None))
        else:
            with working(console, "Reading the recorded issue") as status:
                result = asyncio.run(run(status.set))
    except Exception as exc:
        console.print(f"[red]✗ Promotion failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    if dry_run:
        console.print("[dim]Dry run complete.[/dim]")
    elif daemon.last_artifact_kind == ARTIFACT_REPORT:
        console.print(
            f"[yellow]No PR opened.[/yellow] The original issue remains open.\n"
            f"[dim]The current analysis was saved at {result}.[/dim]"
        )
    else:
        console.print(f"[green]✓ Fix PR:[/green] {truncate(result, 200, '…')}")
