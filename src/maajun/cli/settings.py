"""The welcome callback and settings housekeeping: `config` and `reset`.

Named settings, not config, so it does not collide with `maajun.config` —
that module holds the models, this one holds the commands that edit them."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.cli._shared import (
    app,
    configured_providers,
    console,
    prompt_line,
)
from maajun.config import (
    Config,
    default_config_path,
    default_data_dir,
    render_config,
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """Maajun - AI-powered developer assistant"""
    if ctx.invoked_subcommand is not None:
        return

    auth = AuthManager()
    configured = configured_providers(auth)

    if not configured:
        console.print(Panel(
            "[bold]Welcome to Maajun![/bold]\n\n"
            "Nothing is configured yet. One command sets everything up:\n\n"
            "  [cyan]maajun setup[/cyan]\n\n"
            "It needs an AI API key; everything else is optional.",
            title="Setup Required",
            border_style="yellow",
        ))
    else:
        console.print(Panel(
            f"[green]✓ Configured:[/green] {', '.join(configured)}",
            title="Maajun",
            border_style="green",
        ))
        console.print("\n[bold]Commands:[/bold]\n")
        for name, help_text in _command_summaries(ctx):
            console.print(f"  [cyan]{name:<18}[/cyan]{help_text}")
        console.print("\n  Run [bold]maajun <command> --help[/bold] for details.\n")


def _command_summaries(ctx: typer.Context) -> list[tuple[str, str]]:
    """(name, one-line help) for every registered command.

    Generated from the Typer app rather than hand-maintained: the previous
    hard-coded list had already drifted, omitting report and sign-out.
    """
    command = typer.main.get_command(app)
    # `setup` leads: the whole point is that one command does everything.
    names = sorted(command.list_commands(ctx), key=lambda n: (n != "setup", n))
    summaries = []
    for name in names:
        subcommand = command.get_command(ctx, name)
        if subcommand is None or subcommand.hidden:
            continue
        summaries.append((name, (subcommand.get_short_help_str(limit=60) or "")))
    return summaries


@app.command()
def config(
    key: str | None = typer.Argument(None, help="Config key (e.g., 'github.mode')"),
    value: str | None = typer.Argument(None, help="Value to set"),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """View or set configuration values.

    Examples:
      maajun config                      # Show all config
      maajun config github.mode          # Show mode
      maajun config github.mode fix      # Set mode to fix
      maajun config monitor.log_files /var/log/app/error.log,/var/log/app2/error.log
    """
    config = Config.load(config_path)

    if key is None:
        console.print(Panel("[bold]Current Configuration[/bold]", border_style="blue"))
        console.print(render_config(config))
        return

    if value is None:
        try:
            val = config.get(key)
            console.print(f"[green]{key}[/green] = [bold]{val}[/bold]")
        except ValueError as e:
            console.print(f"[red]✗ {e}[/red]")
            raise typer.Exit(1) from e
        return

    try:
        config.set(key, value)
        config.save(config_path)
        console.print(f"[green]✓ Set {key} = {value}[/green]")
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e


@app.command()
def reset(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete all maajun data — config, credentials, incidents, workspaces"""
    config_dir = default_config_path().parent
    data_dir = default_data_dir()

    # Honour a custom daemon.workdir so clones/incident DB are actually
    # removed even when they live outside the default data dir.
    dirs = [config_dir, data_dir]
    try:
        workdir = Path(Config.load().daemon.workdir).expanduser()
        if workdir not in dirs:
            dirs.append(workdir)
    except Exception:
        pass  # unreadable config — fall back to the defaults above

    if not force:
        dir_lines = "\n".join(f"  • [cyan]{d}[/cyan]" for d in dirs)
        console.print(Panel(
            "[bold]This will delete:[/bold]\n\n"
            f"{dir_lines}\n"
            "  • Credentials (API keys + GitHub token)\n\n"
            "[yellow]This cannot be undone.[/yellow]",
            title="Reset Maajun",
            border_style="red",
        ))
        confirm = prompt_line("> Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            console.print("[dim]Cancelled.[/dim]")
            return

    for d in dirs:
        if d.exists():
            shutil.rmtree(d)
            console.print(f"[green]✓ Removed {d}[/green]")

    auth = AuthManager()
    auth.clear_all()
    console.print("[green]✓ Cleared all credentials[/green]")

    console.print(
        "\n[bold]Maajun has been reset.[/bold]\n\n"
        "Run [bold]maajun init[/bold] to start fresh.\n"
    )
