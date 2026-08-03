"""Setup & configuration commands: the welcome callback, init,
config, and reset."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.cli._shared import (
    _configured_providers,
    _implemented_providers,
    _input,
    _prompt_mode,
    app,
    console,
)
from maajun.config import (
    STARTER_CONFIG,
    AIProviderConfig,
    Config,
    GitHubConfig,
    MonitorConfig,
    default_config_path,
    default_data_dir,
    render_config,
)
from maajun.utils import PLACEHOLDER_REPO, is_valid_repo


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """Maajun - AI-powered developer assistant"""
    if ctx.invoked_subcommand is not None:
        return

    auth = AuthManager()
    configured = _configured_providers(auth)

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
def init(
    path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", help="Prompt for settings"
    ),
):
    """Write a starter config file for the monitoring daemon"""
    path = path or default_config_path()
    if path.exists():
        console.print(f"[yellow]⚠ {path} already exists.[/yellow]")
        overwrite = _input("> Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            console.print("[dim]Cancelled.[/dim]")
            return

    if interactive:
        console.print(Panel(
            "[bold]Maajun Init[/bold]\n\nLet's set up your daemon config.",
            border_style="blue",
        ))

        # AI provider — default to a configured one, else the built-in default.
        auth = AuthManager()
        configured = _configured_providers(auth)
        providers = _implemented_providers()
        default_provider = configured[0] if configured else providers[0]
        provider = _input(
            f"> AI provider ({'/'.join(providers)}) [{default_provider}]: "
        ).strip() or default_provider
        if provider not in providers:
            console.print(
                f'[yellow]⚠ Unknown provider "{provider}". Using {default_provider}.[/yellow]'
            )
            provider = default_provider
        if provider not in configured:
            console.print(
                f"[dim]No API key stored for {provider} yet — "
                "run 'maajun login' before 'maajun watch'.[/dim]"
            )

        console.print("\n[bold]Repository[/bold] (where maajun will open PRs)")
        repo = _input(
            f"> Repository (owner/name) [{PLACEHOLDER_REPO}]: "
        ).strip() or PLACEHOLDER_REPO
        if not is_valid_repo(repo):
            console.print(
                f'[yellow]⚠ "{repo}" is not in owner/name form. Using placeholder.[/yellow]'
            )
            repo = PLACEHOLDER_REPO

        base_branch = _input("> Base branch [main]: ").strip() or "main"

        mode = _prompt_mode("suggest")

        console.print("\n[bold]Log Files[/bold] (comma-separated paths to monitor)")
        log_files_input = _input("> Log files [/var/log/myapp/error.log]: ").strip()
        log_files = (
            [lf.strip() for lf in log_files_input.split(",") if lf.strip()]
            if log_files_input else ["/var/log/myapp/error.log"]
        )

        poll_input = _input("> Poll interval in seconds [30]: ").strip() or "30"
        try:
            poll_interval = float(poll_input)
        except ValueError:
            poll_interval = 30.0

        config = Config(
            ai=AIProviderConfig(provider=provider),
            github=GitHubConfig(
                repo=repo,
                base_branch=base_branch,
                mode=mode,
            ),
            monitor=MonitorConfig(
                log_files=log_files,
                poll_interval=poll_interval,
            ),
        )
        config.save(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_CONFIG)

    console.print(f"\n[green]✓ Wrote {path}[/green]")

    if interactive:
        console.print(
            "\n[bold]Next steps:[/bold]\n"
            "  1. Run [bold]maajun login[/bold] if you haven't stored an API key\n"
            "  2. Run [bold]maajun setup[/bold] to connect GitHub and error sources\n"
            "  3. Run [bold]maajun watch[/bold] to start monitoring\n"
            "\n[dim]Tips: 'maajun config' views/changes settings; "
            "'maajun add-repo' watches more repos.[/dim]"
        )
    else:
        console.print(
            "\n[bold]Next steps:[/bold]\n"
            "  1. Edit the config: set [cyan]monitor.log_files[/cyan] (or GitHub Actions)\n"
            "  2. Run [bold]maajun setup[/bold] to connect GitHub and error sources\n"
            "  3. Run [bold]maajun watch[/bold] to start monitoring\n"
        )



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
        confirm = _input("> Type 'yes' to confirm: ").strip().lower()
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
