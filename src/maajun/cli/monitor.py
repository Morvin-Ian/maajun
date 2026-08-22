from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.cli.shared import app, console, load_config, pick_repo, split_list
from maajun.cli.status_checks import build_status, gather_github
from maajun.config import RepoConfig
from maajun.daemon import build_daemon, build_daemon_for_report
from maajun.discovery import probe_source
from maajun.progress import WorkingStatus, working
from maajun.utils import is_valid_repo, truncate
from maajun.vcs import GitHubClient

NOTICE_STYLES = {"info": "cyan", "success": "green", "warn": "yellow", "error": "red"}


def watch_with_spinner(daemon, *, once: bool) -> None:
    """Run the daemon under a live spinner, printing notices above it."""
    status = WorkingStatus("Watching for errors")

    def notice(message: str, level: str) -> None:
        # Escaped: a stray closing tag in a log line is a MarkupError.
        style = NOTICE_STYLES.get(level, "dim")
        console.print(f"[{style}]{escape(message)}[/{style}]")

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
    """Run the monitoring daemon: watch error sources and document what turns up."""
    use_spinner = not verbose and not dry_run and sys.stdin.isatty()
    logging.basicConfig(
        level=logging.DEBUG if verbose else (logging.WARNING if use_spinner else logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(config_path)

    if mode:
        if mode not in ("suggest", "fix"):
            console.print(f"[red]✗ Invalid mode: {mode}. Use 'suggest' or 'fix'.[/red]")
            raise typer.Exit(1)
        # Local mode has no repos to write to, so -m fix does nothing.
        if not config.github.repos:
            console.print(
                f"[yellow]⚠ --mode {mode} has no effect without a configured "
                "repository — local mode only writes reports to disk. "
                "Add one with 'maajun add-repo <owner/name>'.[/yellow]"
            )
        for repo_config in config.github.repos:
            repo_config.mode = mode

    try:
        daemon = build_daemon(config)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    repos = config.github.get_all_repos()
    dry_note = "\n[yellow]Dry run — no branches/PRs will be created[/yellow]" if dry_run else ""
    mode_source = " (override)" if mode else ""
    if daemon.local_mode:
        console.print(Panel(
            f"[bold]Maajun watch[/bold] [dim](local — no GitHub repo configured)[/dim]\n\n"
            f"Analyzing: [cyan]{daemon.workspaces[''].path}[/cyan]\n"
            f"Reports:   [cyan]{daemon.report_dir}[/cyan]\n"
            f"Monitors:  {', '.join(m.name for m in daemon.monitors)}\n"
            f"Interval:  {config.monitor.poll_interval}s" + dry_note
            + "\n\n[dim]Run 'maajun setup' to connect a repo and open PRs instead.[/dim]",
            border_style="blue",
        ))
    elif len(repos) > 1:
        repos_text = "\n".join(
            f"  • [cyan]{repo_config.repo}[/cyan] "
            f"(base: {repo_config.base_branch}, mode: {repo_config.mode})"
            for repo_config in repos
        )

        def repo_of(monitor) -> str:
            repo_config = daemon.monitor_to_repo.get(id(monitor))
            return repo_config.repo if repo_config else "unknown"

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
        repo_config = repos[0]
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
            watch_with_spinner(daemon, once=once)
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

    config = load_config(config_path)

    if mode:
        if mode not in ("suggest", "fix"):
            console.print(f"[red]✗ Invalid mode: {mode}. Use 'suggest' or 'fix'.[/red]")
            raise typer.Exit(1)
        # Local mode has no repos to write to, so -m fix does nothing.
        if not config.github.repos:
            console.print(
                f"[yellow]⚠ --mode {mode} has no effect without a configured "
                "repository — local mode only writes reports to disk. "
                "Add one with 'maajun add-repo <owner/name>'.[/yellow]"
            )
        for repo_config in config.github.repos:
            repo_config.mode = mode

    try:
        daemon = build_daemon_for_report(config)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    repos = daemon.repo_configs
    if repo and daemon.local_mode:
        console.print(
            f"[red]✗ --repo {repo} given but no GitHub repo is configured. "
            "Run 'maajun setup' first.[/red]"
        )
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
        target = pick_repo(repos)
    else:
        console.print("[red]✗ Multiple repos configured. Use --repo to specify which one.[/red]")
        console.print("[dim]Available repos:[/dim]")
        for repo_config in repos:
            console.print(f"  • {repo_config.repo}")
        raise typer.Exit(1)

    if base_branch:
        target.base_branch = base_branch

    if daemon.local_mode:
        destination = (
            f"Analyzing: [cyan]{daemon.workspaces[''].path}[/cyan]\n"
            f"Report:    [cyan]{daemon.report_dir}[/cyan]"
        )
    else:
        destination = (
            f"Repo:  [cyan]{target.repo}[/cyan] "
            f"(base: {target.base_branch}, mode: {target.mode})"
        )
    console.print(Panel(
        f"[bold]Maajun report[/bold]\n\n"
        f"{destination}\n"
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
            asyncio.run(run_report(lambda phase: None))
            if dry_run:
                console.print("\n[dim]Dry run complete.[/dim]")
        else:
            with working(console, "Preparing workspace") as status:
                result = asyncio.run(run_report(status.set))
            # What was published: fix mode that changed nothing files an issue.
            label = daemon.artifact_label(daemon.last_artifact_kind)
            console.print(f"\n[green]✓ {label}:[/green] {result}")
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
    except Exception as e:
        console.print(f"\n[red]✗ Failed: {e}[/red]")
        raise typer.Exit(1) from e


@app.command(name="add-repo")
def add_repo(
    repo: str = typer.Argument(help="Repository to watch, as owner/name"),
    base_branch: str | None = typer.Option(
        None, "--base-branch", "-b",
        help="Branch to open PRs against (new repos default to main)",
    ),
    mode: str | None = typer.Option(
        None, "--mode", "-m", help="'suggest' or 'fix' (new repos default to suggest)"
    ),
    log_files: str | None = typer.Option(
        None, "--log-files", "-l", help="Comma-separated log paths for this repo"
    ),
    deploy_path: str | None = typer.Option(
        None, "--path", help="The app's folder on the server"
    ),
    port: int | None = typer.Option(None, "--port", help="Port the app listens on"),
    runs: str | None = typer.Option(
        None, "--runs", help="How it runs, e.g. 'docker compose' or 'systemd'"
    ),
    journald_units: str | None = typer.Option(
        None, "--journald-units", help="Comma-separated systemd units to read"
    ),
    docker_containers: str | None = typer.Option(
        None, "--docker-containers", help="Comma-separated containers to read logs from"
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Add a repository to watch.

    Re-adding a repo already in the list updates only the settings you pass,
    leaving its other settings alone.
    """
    if not is_valid_repo(repo):
        console.print(f'[red]✗ "{repo}" is not in owner/name form.[/red]')
        raise typer.Exit(1)
    if mode is not None and mode not in ("suggest", "fix"):
        console.print(f"[red]✗ Invalid mode: {mode}. Use 'suggest' or 'fix'.[/red]")
        raise typer.Exit(1)

    config = load_config(config_path)
    logs = split_list(log_files)
    units = split_list(journald_units)
    containers = split_list(docker_containers)

    # None means "leave as is", not "reset to the default" — changing the
    # mode used to silently revert the base branch.
    entry = next((rc for rc in config.github.repos if rc.repo == repo), None)
    updated = entry is not None
    if entry is None:
        entry = RepoConfig(repo=repo)
        config.add_repo(entry)
    if base_branch is not None:
        entry.base_branch = base_branch
    if mode is not None:
        entry.mode = mode
    if logs is not None:
        entry.log_files = logs
    if deploy_path is not None:
        entry.deployment.path = deploy_path
    if port is not None:
        entry.deployment.port = port
    if runs is not None:
        entry.deployment.runs = runs
    if units is not None:
        entry.deployment.journald_units = units
    if containers is not None:
        entry.deployment.docker_containers = containers
    config.save(config_path)

    names = ", ".join(repo_config.repo for repo_config in config.github.repos)
    verb = "Updated" if updated else "Added"
    console.print(f"[green]✓ {verb} {repo} ({entry.mode}).[/green]")
    console.print(f"[dim]Now watching: {names}[/dim]")


def print_check(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
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
        False, "--no-network",
        help="Skip GitHub reachability checks, and probing docker/systemd",
    ),
):
    """Check that credentials, repos, and log files are ready for `watch`."""
    config = load_config(config_path)
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
        probe=None if no_network else probe_source,
    )

    console.print(Panel("[bold]Maajun status[/bold]", border_style="blue"))
    for section in sections:
        console.print(f"\n[bold]{section.title}[/bold]")
        for check in section.checks:
            print_check(check.label, check.ok, check.detail, check.warn)

    if ok:
        console.print("\n[green]✓ Ready. Run [bold]maajun watch[/bold].[/green]")
    else:
        console.print(
            "\n[yellow]Some checks failed — fix the ✗ items above "
            "before running watch.[/yellow]"
        )
        raise typer.Exit(1)
