from __future__ import annotations

import logging
from pathlib import Path

import typer

from maajun.auth import AuthManager
from maajun.cli.shared import (
    app,
    configured_providers,
    console,
    implemented_providers,
    load_config,
)


@app.command()
def chat(
    config_path: Path | None = typer.Option(
        None, "--config", "-c", help="Config file location"
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="Override the configured AI provider"
    ),
    thinking: bool = typer.Option(
        False, "--thinking", help="Use the provider's reasoning model"
    ),
    session: int | None = typer.Option(
        None, "--session", "-s", help="Carry on an earlier session"
    ),
    prompt: str = typer.Option(
        "", "--prompt", "-p",
        help="Answer one question and exit, instead of opening the REPL",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
):
    """Talk to maajun: ask what it can do, have it do it, or recall past work."""
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    config = load_config(config_path)
    auth = AuthManager()

    if provider:
        implemented = implemented_providers()
        if provider not in implemented:
            console.print(
                f"[red]✗ Unknown provider {provider!r}. "
                f"Choose one of: {', '.join(implemented)}[/red]"
            )
            raise typer.Exit(1)
        config.ai.provider = provider
    if thinking:
        config.ai.thinking_mode = True

    api_key = auth.get_api_key(config.ai.provider)
    if not api_key:
        configured = configured_providers(auth)
        hint = (
            f" Configured: {', '.join(configured)} — try --provider."
            if configured
            else " Run 'maajun setup' to store one."
        )
        console.print(
            f"[red]✗ No API key for {config.ai.provider}.[/red][dim]{hint}[/dim]"
        )
        raise typer.Exit(1)

    config.ai.api_key = api_key

    workdir = Path(config.daemon.workdir).expanduser()
    # Imported here: chat.session imports cli.shared, which runs this
    # package's __init__, which imports this module.
    from maajun.chat.session import run_chat_session

    try:
        run_chat_session(
            config,
            console=console,
            workdir=workdir,
            resume=session,
            prompt=prompt.strip(),
        )
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")
