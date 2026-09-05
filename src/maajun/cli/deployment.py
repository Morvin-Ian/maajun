from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.cli.shared import app, console, load_config
from maajun.config import Config, RepoConfig
from maajun.project.discovery import Discovered, discover
from maajun.project.inspection import Inspection, inspect_repo


def describe_found(found: Discovered) -> str:
    """The panel body for one discovery result."""
    parts = []
    if found.path:
        parts.append(f"Folder: [cyan]{found.path}[/cyan]")
    if found.port:
        parts.append(f"Port:   [cyan]{found.port}[/cyan]")
    if found.runs:
        parts.append(f"Runs:   [cyan]{found.runs}[/cyan]")
    if found.service_command:
        parts.append(f"Command: [cyan]{found.service_command}[/cyan]")
    if found.proxy_config_path:
        parts.append(
            f"Proxy:  [cyan]{found.proxy_config_path}[/cyan] "
            f"([yellow]{found.config_owner}[/yellow])"
        )
    if found.proxy_body_limit:
        parts.append(f"Proxy request limit: [cyan]{found.proxy_body_limit}[/cyan]")
    if found.has_source():
        sources = []
        for kind, targets in (
            ("file", found.log_files),
            ("journald", found.journald_units),
            ("docker", found.docker_containers),
        ):
            sources.extend(f"  • {kind}: [cyan]{t}[/cyan]" for t in targets)
        parts.append("Errors would be read from:\n" + "\n".join(sources))
    else:
        parts.append(
            "[yellow]No runtime error source found.[/yellow] Runtime errors "
            "are the 500s your users hit — without one, maajun only sees CI."
        )
    if found.notes:
        parts.append(
            "[dim]How:\n"
            + "\n".join(f"  {note}" for note in found.notes)
            + "[/dim]"
        )
    return "\n\n".join(parts)


def describe_inspection(inspection: Inspection) -> str:
    """The panel body for what reading the code turned up."""
    parts = []
    if inspection.stack:
        parts.append(f"Stack:      [cyan]{inspection.stack}[/cyan]")
    if inspection.entrypoint:
        parts.append(f"Entrypoint: [cyan]{inspection.entrypoint}[/cyan]")
    if inspection.log_files:
        written = "\n".join(f"  • [cyan]{path}[/cyan]" for path in inspection.log_files)
        parts.append(f"Errors are logged to:\n{written}")
    if inspection.logging_gaps:
        gaps = "\n".join(f"  • {gap}" for gap in inspection.logging_gaps)
        parts.append(f"[yellow]Errors that would be missed:[/yellow]\n{gaps}")
    if inspection.logging_advice:
        parts.append(
            "[yellow]To catch them:[/yellow]\n" + inspection.logging_advice
        )
    if inspection.risky_areas:
        risky = "\n".join(f"  • {area}" for area in inspection.risky_areas)
        parts.append(f"[dim]Where bugs are likely:\n{risky}[/dim]")
    if inspection.confidence:
        parts.append(
            f"[dim]Confidence: {inspection.confidence} · "
            f"cost ${inspection.cost_usd:.4f}[/dim]"
        )
    return "\n\n".join(parts) or "[dim]Nothing conclusive from the code.[/dim]"


def tuning_advice(inspection: Inspection) -> list[str]:
    """Commands that apply what the code says about its log format.

    Printed rather than written: an error pattern decides what the daemon
    reacts to for every repo, which is not a change to make behind someone's
    back on the strength of one reading.
    """
    commands = []
    if inspection.json_level_field:
        commands.append(
            f"maajun config monitor.json_level_field {inspection.json_level_field}"
        )
    if inspection.error_pattern:
        commands.append(f"maajun config monitor.error_pattern '{inspection.error_pattern}'")
    return commands


def apply_discovery(entry: RepoConfig, found: Discovered) -> None:
    """Fold a discovery into a repo entry, keeping what is already set."""
    entry.deployment = found.merged_into(entry.deployment)


def apply_inspection(entry: RepoConfig, inspection: Inspection) -> None:
    """Fold what the code says into a repo entry, keeping what is already set."""
    deployment = entry.deployment
    deployment.stack = deployment.stack or inspection.stack
    deployment.port = deployment.port or inspection.port
    for path in inspection.log_files:
        if path not in deployment.log_files:
            deployment.log_files = [*deployment.log_files, path]


def analyze_repo(entry: RepoConfig, config: Config, auth: AuthManager) -> Inspection:
    """Read the code for one repo, or explain why it cannot be read."""
    folder = entry.deployment.path
    if not folder or not Path(folder).expanduser().is_dir():
        console.print(
            "  [dim]No local checkout to read — pass --path, or run this on "
            "the server where the app is deployed.[/dim]"
        )
        return Inspection()

    api_key = auth.get_api_key(config.ai.provider)
    if not api_key:
        console.print(
            f"  [yellow]⚠ No {config.ai.provider} API key, so the code was "
            "not read. Run 'maajun setup'.[/yellow]"
        )
        return Inspection()

    ai = config.ai.model_copy(update={"api_key": api_key})
    with console.status(f"[dim]Reading {folder} to see how it fails...[/dim]"):
        return asyncio.run(inspect_repo(folder, ai))


def record_deployment(
    entry: RepoConfig, config: Config, auth: AuthManager
) -> None:
    """Work out where one repo's errors land, and record it. Asks nothing.

    Findings are additive — anything already configured wins — so there is
    nothing to confirm, and no path anyone has to remember and type.
    """
    with console.status(f"[dim]Looking for {entry.repo} on this host...[/dim]"):
        found = discover(entry.repo, entry.deployment)
    console.print(f"  [dim]{entry.repo}[/dim]")
    for note in found.notes:
        console.print(f"    [dim]· {note}[/dim]")
    entry.deployment = found.merged_into(entry.deployment)

    read_the_code(entry, config, auth)

    for kind, target in entry.runtime_sources():
        console.print(f"    [green]✓[/green] watching {kind}: {target}")
    if entry.runtime_sources() or entry.deployment.runtime == "none":
        return
    console.print(
        f"  [yellow]⚠ Nothing watches {entry.repo} for runtime errors yet.[/yellow]\n"
        f"  [dim]On the server where it runs: maajun discover -r {entry.repo} "
        "--save\n"
        f"  Or name the source: maajun add-repo {entry.repo} "
        "--log-files /path/to/error.log[/dim]"
    )


def read_the_code(entry: RepoConfig, config: Config, auth: AuthManager) -> None:
    """Ask the AI where this app's errors surface, and record what it finds.

    The host probes see what is running; only the code says whether a 500
    ever reaches a file. Skipped without a checkout to read or a key to read
    it with.
    """
    folder = entry.deployment.path
    if not folder or not Path(folder).expanduser().is_dir():
        return
    api_key = auth.get_api_key(config.ai.provider)
    if not api_key:
        return
    ai = config.ai.model_copy(update={"api_key": api_key})
    try:
        with console.status(f"[dim]Reading {folder}...[/dim]"):
            inspection = asyncio.run(inspect_repo(folder, ai))
    except Exception as e:
        # Setup is not the place to fail over an optional extra.
        console.print(f"  [yellow]⚠ Could not read the code: {e}[/yellow]")
        return

    if inspection.stack:
        console.print(f"    [dim]· {inspection.stack}[/dim]")
    apply_inspection(entry, inspection)
    for gap in inspection.logging_gaps[:3]:
        console.print(f"    [yellow]⚠[/yellow] [dim]{gap}[/dim]")
    if inspection.logging_advice:
        console.print(f"  [yellow]To catch those:[/yellow] {inspection.logging_advice}")


@app.command(name="discover")
def discover_command(
    repo: str | None = typer.Option(
        None, "--repo", "-r", help="Only probe this repository (owner/name)"
    ),
    save: bool = typer.Option(
        False, "--save", help="Write what was found into the config"
    ),
    analyze: bool = typer.Option(
        True, "--analyze/--no-analyze",
        help="Also read the code with AI to find where its errors surface",
    ),
    path: str | None = typer.Option(
        None, "--path", help="Where the app is deployed, when the probe cannot tell"
    ),
    config_path: Path | None = typer.Option(
        None, "--config", "-c", help="Config file location"
    ),
):
    """Work out how each repo is deployed, and how to catch its errors.

    Probes this machine for the app's folder, port, containers and units,
    then reads the code to find where its exceptions are logged — and where
    they are swallowed. Read-only unless you pass --save.
    """
    config = load_config(config_path)
    auth = AuthManager()
    repos = config.github.repos
    if not repos:
        console.print(
            "[yellow]⚠ No repositories configured, so there is no deployment "
            "to look for. Add one with 'maajun add-repo <owner/name>'.[/yellow]"
        )
        raise typer.Exit(1)
    if repo:
        target = next((rc for rc in repos if rc.repo == repo), None)
        if target is None:
            console.print(
                f"[red]✗ Repository '{repo}' is not configured. "
                f"Add it with 'maajun add-repo {repo}'.[/red]"
            )
            raise typer.Exit(1)
        repos = [target]

    if path and len(repos) > 1:
        console.print(
            "[red]✗ --path describes one deployment; pass --repo too.[/red]"
        )
        raise typer.Exit(1)

    advice: list[str] = []
    for entry in repos:
        existing = entry.deployment
        if path:
            existing = existing.model_copy(update={"path": path})
        with console.status(f"[dim]Looking for {entry.repo} on this host...[/dim]"):
            found = discover(entry.repo, existing)
        console.print(Panel(
            describe_found(found),
            title=f"[bold]{entry.repo}[/bold]",
            border_style="blue",
        ))
        if save:
            apply_discovery(entry, found)
            if path:
                entry.deployment.path = path

        if not analyze:
            continue
        # After discovery: it is what finds the folder to read.
        probe_target = entry if save else RepoConfig(
            repo=entry.repo, deployment=found.merged_into(existing)
        )
        inspection = analyze_repo(probe_target, config, auth)
        if not inspection.has_findings():
            continue
        console.print(Panel(
            describe_inspection(inspection),
            title=f"[bold]{entry.repo} — from the code[/bold]",
            border_style="cyan",
        ))
        advice.extend(tuning_advice(inspection))
        if save:
            apply_inspection(entry, inspection)

    if advice:
        console.print("\n[bold]Suggested detection tuning[/bold]")
        for command in dict.fromkeys(advice):
            console.print(f"  [cyan]{command}[/cyan]")

    if not save:
        console.print(
            "\n[dim]Nothing written. Re-run with --save to keep this.[/dim]"
        )
        return

    config.save(config_path)
    console.print("\n[green]✓ Saved.[/green] [dim]Check it with 'maajun status'.[/dim]")
