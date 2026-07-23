"""Monitoring commands: watch, report, add-repo, and the status preflight."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.live import Live
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.checks import build_status, gather_github
from maajun.cli._shared import _pick_repo, app, console
from maajun.config import Config, RepoConfig
from maajun.daemon import build_daemon, build_daemon_for_report
from maajun.progress import WorkingStatus, working
from maajun.utils import is_valid_repo, truncate
from maajun.vcs import GitHubClient

_NOTICE_STYLES = {"info": "cyan", "success": "green", "warn": "yellow", "error": "red"}


def _watch_with_spinner(daemon, *, once: bool) -> None:
    """Run the daemon under a live spinner, printing notices above it."""
    status = WorkingStatus("Watching for errors")

    def notice(message: str, level: str) -> None:
        style = _NOTICE_STYLES.get(level, "dim")
        console.print(f"[{style}]{message}[/{style}]")

    daemon.progress = status.set
    daemon.on_notice = notice
    with Live(status, console=console, refresh_per_second=8, transient=True):
        asyncio.run(daemon.run(once=once, dry_run=False))


@app.command()
def watch(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
    once: bool = typer.Option(False, "--once", help="Run a single poll cycle and exit"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Analyze errors but skip git/PR operations"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
    mode: str | None = typer.Option(
        None, "--mode", "-m", help="Override mode: 'suggest' or 'fix'"
    ),
):
    """Monitor configured error sources; document each new error in a PR"""

    # A live spinner and interleaved log lines fight over the terminal, so the
    # spinner UI is used only for an interactive, non-dry run; otherwise we log.
    use_spinner = not verbose and not dry_run and sys.stdin.isatty()
    logging.basicConfig(
        level=logging.DEBUG if verbose else (logging.WARNING if use_spinner else logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.load(config_path)

    if mode:
        if mode not in ("suggest", "fix"):
            console.print(f"[red]✗ Invalid mode: {mode}. Use 'suggest' or 'fix'.[/red]")
            raise typer.Exit(1)
        config.github.mode = mode
        for rc in config.github.repos:
            rc.mode = mode

    try:
        daemon = build_daemon(config)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    repos = config.github.get_all_repos()
    dry_note = "\n[yellow]Dry run — no branches/PRs will be created[/yellow]" if dry_run else ""
    mode_source = " (override)" if mode else ""
    if len(repos) > 1:
        repos_text = "\n".join(
            f"  • [cyan]{rc.repo}[/cyan] (base: {rc.base_branch}, mode: {rc.mode})"
            for rc in repos
        )

        def repo_of(monitor) -> str:
            rc = daemon.monitor_to_repo.get(monitor.name)
            return rc.repo if rc else "unknown"

        monitors_text = "\n".join(
            f"  • {m.name} → [cyan]{repo_of(m)}[/cyan]" for m in daemon.monitors
        )
        console.print(Panel(
            f"[bold]Maajun watch[/bold] [dim](multi-repo){mode_source}[/dim]\n\n"
            f"Repos:\n{repos_text}\n\n"
            f"Monitors:\n{monitors_text}\n\n"
            f"Interval: {config.monitor.poll_interval}s" + dry_note,
            border_style="blue",
        ))
    else:
        repo_config = repos[0] if repos else config.github
        console.print(Panel(
            f"[bold]Maajun watch[/bold]{mode_source}\n\n"
            f"Repo:     [cyan]{repo_config.repo}[/cyan] "
            f"(base: {repo_config.base_branch})\n"
            f"Mode:     [cyan]{repo_config.mode}[/cyan]\n"
            f"Monitors: {', '.join(m.name for m in daemon.monitors)}\n"
            f"Interval: {config.monitor.poll_interval}s" + dry_note,
            border_style="blue",
        ))

    try:
        if use_spinner:
            _watch_with_spinner(daemon, once=once)
        else:
            asyncio.run(daemon.run(once=once, dry_run=dry_run))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@app.command()
def report(
    description: str = typer.Argument(help="Issue description to investigate"),
    repo: str | None = typer.Option(
        None, "--repo", "-r",
        help="Target repository (owner/name). Required when multiple repos are configured.",
    ),
    base_branch: str | None = typer.Option(
        None, "--base-branch", "-b",
        help="Branch to base the report on. Defaults to repo's configured branch.",
    ),
    mode: str | None = typer.Option(
        None, "--mode", "-m", help="Override mode: 'suggest' or 'fix'"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Analyze but skip git/PR operations"
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Show debug output"),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Describe an issue; maajun analyzes the code and opens a PR with a report."""
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    config = Config.load(config_path)

    if mode:
        if mode not in ("suggest", "fix"):
            console.print(f"[red]✗ Invalid mode: {mode}. Use 'suggest' or 'fix'.[/red]")
            raise typer.Exit(1)
        config.github.mode = mode
        for rc in config.github.repos:
            rc.mode = mode

    repos = config.github.get_all_repos()
    if not repos:
        console.print("[red]✗ No repository configured. Run 'maajun init' first.[/red]")
        raise typer.Exit(1)

    if repo:
        target = next((rc for rc in repos if rc.repo == repo), None)
        if not target:
            console.print(
                f"[red]✗ Repository '{repo}' not in config. "
                "Use 'maajun add-repo' first.[/red]"
            )
            raise typer.Exit(1)
    elif len(repos) == 1:
        target = repos[0]
    elif sys.stdin.isatty():
        target = _pick_repo(repos)
    else:
        console.print("[red]✗ Multiple repos configured. Use --repo to specify which one.[/red]")
        console.print("[dim]Available repos:[/dim]")
        for rc in repos:
            console.print(f"  • {rc.repo}")
        raise typer.Exit(1)

    if base_branch:
        target.base_branch = base_branch

    try:
        daemon = build_daemon_for_report(config)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    console.print(Panel(
        f"[bold]Maajun report[/bold]\n\n"
        f"Repo:  [cyan]{target.repo}[/cyan] (base: {target.base_branch}, mode: {target.mode})\n"
        f"Issue: {truncate(description, 120, '...')}"
        + ("\n[yellow]Dry run — no branches/PRs will be created[/yellow]" if dry_run else ""),
        border_style="blue",
    ))

    async def run_report(progress):
        try:
            return await daemon.handle_manual_report(
                description, target, dry_run=dry_run, progress=progress
            )
        finally:
            await daemon.aclose()

    try:
        if dry_run or verbose:
            console.print("\n[dim]Analyzing the issue — this can take a moment…[/dim]")
            asyncio.run(run_report(lambda _phase: None))
            if dry_run:
                console.print("\n[dim]Dry run complete.[/dim]")
        else:
            with working(console, "Preparing workspace") as status:
                pr_url = asyncio.run(run_report(status.set))
            console.print(f"\n[green]✓ PR opened:[/green] {pr_url}")
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
    except Exception as e:
        console.print(f"\n[red]✗ Failed: {e}[/red]")
        raise typer.Exit(1) from e


@app.command(name="add-repo")
def add_repo(
    repo: str = typer.Argument(help="Repository to watch, as owner/name"),
    base_branch: str = typer.Option(
        "main", "--base-branch", "-b", help="Branch to open PRs against"
    ),
    mode: str = typer.Option("suggest", "--mode", "-m", help="'suggest' or 'fix'"),
    log_files: str = typer.Option(
        "", "--log-files", "-l", help="Comma-separated log paths for this repo"
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Add a repository to watch (enables multi-repo mode).

    The first call migrates an existing single-repo config into the repo list.
    """
    if not is_valid_repo(repo):
        console.print(f'[red]✗ "{repo}" is not in owner/name form.[/red]')
        raise typer.Exit(1)
    if mode not in ("suggest", "fix"):
        console.print(f"[red]✗ Invalid mode: {mode}. Use 'suggest' or 'fix'.[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    logs = [lf.strip() for lf in log_files.split(",") if lf.strip()]
    config.add_repo(RepoConfig(
        repo=repo, base_branch=base_branch, mode=mode, log_files=logs,
    ))
    config.save(config_path)

    names = ", ".join(rc.repo for rc in config.github.repos)
    console.print(f"[green]✓ Added {repo} ({mode}).[/green]")
    console.print(f"[dim]Now watching: {names}[/dim]")


def _check(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    """Print one ✓/⚠/✗ status line. Returns ok."""
    if ok:
        mark = "[green]✓[/green]"
    elif warn:
        mark = "[yellow]⚠[/yellow]"
    else:
        mark = "[red]✗[/red]"
    suffix = f" [dim]{detail}[/dim]" if detail else ""
    console.print(f"  {mark} {label}{suffix}")
    return ok


@app.command()
def status(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
    no_network: bool = typer.Option(
        False, "--no-network", help="Skip GitHub reachability checks"
    ),
):
    """Check that credentials, repos, and log files are ready for `watch`."""
    config = Config.load(config_path)
    auth = AuthManager()

    provider = config.ai.provider
    has_key = auth.has_api_key(provider)
    has_token = auth.has_github_token()
    repos = config.github.get_all_repos()

    network = None
    if repos and has_token and not no_network:
        client = GitHubClient(auth.get_github_token())
        with console.status("[dim]Checking GitHub...[/dim]"):
            network = asyncio.run(gather_github(client, repos))

    sections, ok = build_status(
        config, provider=provider, has_key=has_key,
        has_token=has_token, repos=repos, network=network,
    )

    console.print(Panel("[bold]Maajun status[/bold]", border_style="blue"))
    for section in sections:
        console.print(f"\n[bold]{section.title}[/bold]")
        for c in section.checks:
            _check(c.label, c.ok, c.detail, c.warn)

    if ok:
        console.print("\n[green]✓ Ready. Run [bold]maajun watch[/bold].[/green]")
    else:
        console.print(
            "\n[yellow]Some checks failed — fix the ✗ items above "
            "before running watch.[/yellow]"
        )
        raise typer.Exit(1)
