
from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.cli.shared import (
    app,
    configured_providers,
    console,
    load_config,
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
        for name, help_text in command_summaries(ctx):
            console.print(f"  [cyan]{name:<18}[/cyan]{help_text}")
        console.print("\n  Run [bold]maajun <command> --help[/bold] for details.\n")


def command_summaries(ctx: typer.Context) -> list[tuple[str, str]]:
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
    repo: str | None = typer.Option(
        None, "--repo", "-r",
        help="Apply a github.* key to this repository only (owner/name)",
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """View or set configuration values.

    A github.* key with no --repo applies to every configured repository, so
    one command still covers the common case of wanting the same setting
    everywhere.

    Examples:
      maajun config                      # Show all config
      maajun config github.mode          # Show mode
      maajun config github.mode fix      # Set mode to fix, on every repo
      maajun config github.mode fix -r acme/api          # ...on one repo
      maajun config github.test_command "pytest -q" -r acme/api
      maajun config monitor.log_files /var/log/app/error.log,/var/log/app2/error.log
    """
    config = load_config(config_path)

    if key is None:
        console.print(Panel("[bold]Current Configuration[/bold]", border_style="blue"))
        console.print(render_config(config))
        return

    scope = f" [dim](repo: {repo})[/dim]" if repo else ""
    if value is None:
        try:
            val = config.get(key, repo)
            console.print(f"[green]{key}[/green] = [bold]{val}[/bold]{scope}")
        except ValueError as e:
            console.print(f"[red]✗ {e}[/red]")
            raise typer.Exit(1) from e
        return

    try:
        config.set(key, value, repo)
        config.save(config_path)
        console.print(f"[green]✓ Set {key} = {value}[/green]{scope}")
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e


def unsafe_to_delete(path: Path) -> str:
    """Why `path` must not be rmtree'd, or "" if deleting it is reasonable.

    daemon.workdir is a hand-edited string in a TOML file, and reset removes
    it recursively. A typo, a leftover value, or a workdir pointed at a real
    checkout should not cost someone their home directory or their source.
    """
    home = Path.home().resolve()
    if path == Path(path.anchor):
        return "it is a filesystem root."
    if path == home:
        return "it is your home directory."
    if path in home.parents or path in Path.cwd().resolve().parents:
        return "it contains your home directory or working directory."
    if (path / ".git").exists():
        return "it is a git checkout, not a maajun workdir."
    return ""


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
        workdir = Path(Config.load().daemon.workdir).expanduser().resolve()
        if workdir not in dirs:
            refusal = unsafe_to_delete(workdir)
            if refusal:
                # rmtree on a mistyped or over-broad workdir is unrecoverable,
                # and the value comes from a hand-edited TOML file.
                console.print(
                    f"[yellow]⚠ Not deleting daemon.workdir ({workdir}): "
                    f"{refusal} Remove it yourself if that is what you "
                    "want.[/yellow]"
                )
            else:
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
        "Run [bold]maajun setup[/bold] to start fresh.\n"
    )
