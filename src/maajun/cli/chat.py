"""The `maajun chat` command — auth/provider selection, then the chat UI."""

from __future__ import annotations

import typer

from maajun.auth import AuthManager
from maajun.chat_ui import run_chat
from maajun.cli._shared import (
    app,
    build_agent_config,
    configured_providers,
    console,
    pick_provider,
)


@app.command()
def chat(
    provider: str | None = typer.Option(None, "--provider", "-p", help="AI provider to use"),
    thinking: bool = typer.Option(False, "--thinking", help="Enable the provider's reasoning mode"),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        help="Run tools that modify files or execute commands without asking",
    ),
):
    """Start an interactive chat session"""
    auth = AuthManager()
    configured = configured_providers(auth)

    if not configured:
        console.print(
            "[red]No providers configured. Run [bold]maajun login[/bold] to set up a key.[/red]"
        )
        raise typer.Exit(1)

    if provider is None:
        provider = pick_provider(configured)
    elif provider not in configured:
        console.print(f"[red]Provider '{provider}' is not configured.[/red]")
        console.print(f"[dim]Configured: {', '.join(configured)}[/dim]")
        raise typer.Exit(1)

    run_chat(
        build_agent_config(auth, provider, thinking),
        provider,
        auto_approve=auto_approve,
        console=console,
    )
