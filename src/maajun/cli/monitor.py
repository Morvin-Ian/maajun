from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel

from maajun.auth import AuthManager, credentials_file, file_store_in_use
from maajun.cli.deployment import record_deployment
from maajun.cli.shared import (
    Asker,
    app,
    at_a_terminal,
    console,
    load_config,
    pick_repo,
    prompt_mode,
    split_list,
)
from maajun.cli.status_checks import build_status, gather_github
from maajun.config import Config, RepoConfig
from maajun.daemon import build_daemon, build_daemon_for_report, service
from maajun.daemon.store import ARTIFACT_IGNORED
from maajun.discovery import probe_source
from maajun.progress import working
from maajun.utils import is_valid_repo, qualify, truncate
from maajun.vcs import GitHubClient
from maajun.vcs.gh import account_login

NOTICE_STYLES = {"info": "cyan", "success": "green", "warn": "yellow", "error": "red"}


def print_notices(daemon) -> None:
    """Report what the daemon does as it happens, one line at a time.

    No spinner: this output is read live in a terminal and later out of the
    log file, and an animation is noise in both.
    """
    def notice(message: str, level: str) -> None:
        # Escaped: a stray closing tag in a log line is a MarkupError.
        style = NOTICE_STYLES.get(level, "dim")
        console.print(f"[{style}]{escape(message)}[/{style}]")

    daemon.on_notice = notice


async def check_it_runs(config) -> None:
    """Build the daemon and throw it away, to fail in front of the user.

    Closed properly rather than dropped: it owns an HTTP client and the
    incident database.
    """
    daemon = build_daemon(config)
    try:
        await daemon.aclose()
    finally:
        daemon.store.close()


def start_in_background(
    config, config_path, mode: str | None, workdir: str, *, backfill: bool = False
) -> None:
    """Hand the terminal back, with the daemon still running behind it."""
    current = service.running(workdir)
    if current:
        console.print(
            f"[yellow]⚠ Already watching (pid {current.pid}).[/yellow]\n"
            f"[dim]Logs: {current.log_file} · stop it with 'maajun watch --stop'[/dim]"
        )
        raise typer.Exit(1)

    try:
        asyncio.run(check_it_runs(config))
    except RuntimeError as e:
        # Fail here, in front of the user, rather than in a log file nobody
        # is watching yet.
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    args: list[str] = []
    if config_path:
        args += ["--config", str(config_path)]
    if mode:
        args += ["--mode", mode]
    if backfill:
        args.append("--backfill")
    started = service.start(workdir, args)
    console.print(
        f"[green]✓ Watching in the background[/green] [dim](pid {started.pid})[/dim]\n\n"
        f"  Logs:   [cyan]tail -f {started.log_file}[/cyan]\n"
        f"  Status: [cyan]maajun watch --status[/cyan]\n"
        f"  Stop:   [cyan]maajun watch --stop[/cyan]"
    )


def report_daemon_status(workdir: str) -> None:
    current = service.running(workdir)
    if current is None:
        console.print("[dim]Not running.[/dim] Start it with 'maajun watch'.")
        return
    console.print(
        f"[green]✓ Watching[/green] [dim](pid {current.pid})[/dim]\n"
        f"[dim]{current.log_file}[/dim]"
    )
    recent = service.tail(current.log_file)
    if recent:
        console.print(Panel(escape(recent), title="Recent output", border_style="blue"))


def monitors_of(repo_config, daemon) -> list[str]:
    """Names of the monitors feeding one repo.

    Every monitor is a runtime source now that CI is not watched, so there is
    nothing left to filter out.
    """
    return daemon.monitors_for(repo_config)


def deployment_line(deployment) -> str:
    """A one-line "where it runs", or "" when nothing has been recorded."""
    where = deployment.path or ""
    if deployment.port:
        where = f"{where}:{deployment.port}" if where else f"port {deployment.port}"
    parts = [part for part in (where, deployment.runs) if part]
    return " — ".join(parts)


def repo_block(repo_config, daemon) -> str:
    """One repo's line in the watch banner: what it is, and what watches it."""
    lines = [
        f"[cyan]{repo_config.repo}[/cyan] "
        f"(base: {repo_config.base_branch}, mode: {repo_config.mode})"
    ]
    deployed = deployment_line(repo_config.deployment)
    if deployed:
        lines.append(f"  Deployed: {deployed}")
    watching = monitors_of(repo_config, daemon)
    lines.append(f"  Monitors: {', '.join(watching) if watching else 'none'}")
    return "\n".join(lines)


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
    foreground: bool = typer.Option(
        False, "--foreground", "-f",
        help="Stay attached to this terminal instead of running in the background",
    ),
    stop_daemon: bool = typer.Option(
        False, "--stop", help="Stop the daemon running in the background"
    ),
    show_status: bool = typer.Option(
        False, "--status", help="Say whether the daemon is running, and show recent output"
    ),
    backfill: bool = typer.Option(
        False, "--backfill",
        help="Also work through the errors already in the logs, once",
    ),
):
    """Watch for errors in the background, and document what turns up.

    Runs detached by default: the terminal comes straight back and the daemon
    keeps working, logging to <workdir>/watch.log. `--stop` ends it,
    `--status` checks on it, `--foreground` keeps it attached.
    """
    config = load_config(config_path)
    workdir = config.daemon.workdir

    if show_status:
        report_daemon_status(workdir)
        return
    if stop_daemon:
        stopped = service.stop(workdir)
        if stopped is None:
            console.print("[dim]Not running.[/dim]")
        else:
            console.print(f"[green]✓ Stopped maajun watch (pid {stopped}).[/green]")
        return

    detach = not foreground and not once and not dry_run and not verbose
    if detach:
        start_in_background(config, config_path, mode, workdir, backfill=backfill)
        return

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
        daemon = build_daemon(config, backfill=backfill)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    repos = config.github.repos
    dry_note = "\n[yellow]Dry run — no branches/PRs will be created[/yellow]" if dry_run else ""
    if backfill:
        dry_note += (
            "\n[yellow]Backfill — errors already in the logs are analyzed "
            "too[/yellow]"
        )
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
    else:
        scope = " [dim](multi-repo)[/dim]" if len(repos) > 1 else ""
        console.print(Panel(
            f"[bold]Maajun watch[/bold]{scope}{mode_source}\n\n"
            + "\n\n".join(
                repo_block(repo_config, daemon) for repo_config in repos
            )
            + f"\n\nInterval: {config.monitor.poll_interval}s" + dry_note,
            border_style="blue",
        ))
        for repo_config in repos:
            if not monitors_of(repo_config, daemon):
                # Loud, but not fatal: a daemon that refuses to start on a
                # config that worked yesterday is worse than a noisy one.
                console.print(
                    f"[yellow]⚠ Nothing watches {repo_config.repo} for runtime "
                    "errors — its failed requests will go unreported. Run "
                    f"'maajun discover --repo {repo_config.repo} --save'.[/yellow]"
                )

    print_notices(daemon)
    if not once:
        service.write_pid(workdir, os.getpid())
    try:
        asyncio.run(daemon.run(once=once, dry_run=dry_run))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        if not once:
            service.clear_pid(workdir)


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
            if not result and daemon.last_artifact_kind == ARTIFACT_IGNORED:
                console.print(
                    f"\n[yellow]○ Not filed[/yellow] — {daemon.last_ignored_reason}"
                )
                console.print(
                    "[dim]The code did what it is built to do, so there is "
                    "nothing to fix. See it with 'maajun incidents "
                    "--ignored'.[/dim]"
                )
            else:
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
    test_command: str | None = typer.Option(
        None, "--test-command",
        help="Command that verifies a fix-mode edit, e.g. 'pytest -q'",
    ),
    verification_commands: list[str] | None = typer.Option(
        None, "--verify-command",
        help="Post-fix command to run independently; repeat for more commands",
    ),
    reproduction_command: str | None = typer.Option(
        None, "--reproduction-command",
        help="Command expected to fail before a fix and pass after it",
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive",
        help="Never prompt; take everything from flags and the host probes",
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Add a repository to watch.

    Runs the same steps `maajun setup` runs for its first repo: asks for the
    base branch and mode, then probes this host for where the app runs and
    where its errors land. Anything given as a flag is not asked about, and
    with --non-interactive nothing is asked at all.

    The owner can be left off once GitHub is authenticated: `add-repo myapp`
    becomes `<your-login>/myapp`. Re-adding a repo already in the list
    updates only the settings you pass, leaving its other settings alone.
    """
    if "/" not in repo:
        owner = account_login(AuthManager().get_github_token())
        if not owner:
            console.print(
                f'[red]✗ "{repo}" has no owner, and maajun is not signed in '
                "to GitHub to fill one in. Run 'maajun login', or pass "
                "owner/name.[/red]"
            )
            raise typer.Exit(1)
        repo = qualify(repo, owner)
        console.print(f"[dim]Using {repo}[/dim]")
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
    if test_command is not None:
        entry.test_command = test_command
    if verification_commands is not None:
        entry.verification_commands = verification_commands
    if reproduction_command is not None:
        entry.reproduction_command = reproduction_command

    # Prompting is off unless a person is actually at the terminal. add-repo
    # is used from scripts and from the chat tool, and a prompt there would
    # hang waiting on a stdin nobody is typing into.
    ask = Asker(interactive=not non_interactive and at_a_terminal())
    configure_repo(
        entry,
        config,
        AuthManager(),
        ask,
        asked_branch=base_branch,
        asked_mode=mode,
        asked_test_command=test_command,
    )

    config.save(config_path)

    names = ", ".join(repo_config.repo for repo_config in config.github.repos)
    verb = "Updated" if updated else "Added"
    console.print(f"[green]✓ {verb} {repo} ({entry.mode}).[/green]")
    console.print(f"[dim]Now watching: {names}[/dim]")


def configure_repo(
    entry: RepoConfig,
    config: Config,
    auth: AuthManager,
    ask: Asker,
    *,
    asked_branch: str | None,
    asked_mode: str | None,
    asked_test_command: str | None,
) -> None:
    """Fill in a repo the way `maajun setup` fills in its first one.

    Setup asks for the branch and mode, then probes the host to find where the
    app runs and where its errors land. add-repo used to do neither, so a repo
    added afterwards had an empty deployment block and the daemon could only
    see what GitHub showed it. Same steps, same order, same functions.

    Only asks about what was not passed as a flag.
    """
    if ask.interactive and asked_branch is None:
        entry.base_branch = ask.text("Base branch", entry.base_branch)
    if ask.interactive and asked_mode is None:
        entry.mode = prompt_mode(entry.mode)

    # Only fix mode produces a diff, so only fix mode has anything to verify.
    if entry.mode == "fix" and asked_test_command is None and ask.interactive:
        console.print(
            "  [dim]Fix mode edits code. A test command lets maajun verify "
            "the fix and put the result in the PR.[/dim]"
        )
        entry.test_command = ask.text(
            "Test command (Enter to skip)", entry.test_command
        )
    if entry.mode == "fix" and not (
        entry.post_fix_commands() or entry.reproduction_command
    ):
        console.print(
            "  [yellow]⚠ No test command or other verification commands — "
            "fix-mode PRs will be marked "
            "unverified.[/yellow]"
        )

    # The step that was missing: find the deployment on this host and record
    # it. Additive — anything already set, including the flags above, wins.
    record_deployment(entry, config, auth)

    # A repo with no runtime source is the case add-repo silently produced.
    # Ask rather than leave it half-configured.
    if not entry.runtime_sources() and ask.interactive:
        console.print(
            "  [dim]Nothing here watches this app's runtime errors yet. "
            "If it writes an error log, naming it closes that gap.[/dim]"
        )
        answer = ask.text("Log files to watch (comma-separated, Enter to skip)", "")
        named = [path.strip() for path in answer.split(",") if path.strip()]
        if named:
            entry.log_files = named
            for path in named:
                exists = Path(path).expanduser().exists()
                mark = "[green]✓[/green]" if exists else "[yellow]⚠[/yellow]"
                suffix = "" if exists else " [dim](not found yet)[/dim]"
                console.print(f"    {mark} {path}{suffix}")


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
    repos = config.github.repos

    network = None
    if repos and has_token and not no_network:
        client = GitHubClient(auth.get_github_token())
        with console.status("[dim]Checking GitHub...[/dim]"):
            network = asyncio.run(gather_github(client, repos))

    sections, ok = build_status(
        config, provider=provider, has_key=has_key,
        has_token=has_token, repos=repos, network=network,
        probe=None if no_network else probe_source,
        token_source=auth.github_token_source(),
        key_detail=(
            f"in {credentials_file()}" if file_store_in_use() else ""
        ),
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
