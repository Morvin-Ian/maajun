"""Credential commands: login, provider-list, key management, sign-out."""

from __future__ import annotations

import asyncio

import typer
from rich.panel import Panel
from rich.table import Table

from maajun.auth import AuthManager
from maajun.cli._shared import (
    _build_config,
    _implemented_providers,
    _input,
    _secret_input,
    app,
    console,
)
from maajun.providers.base import ProviderType
from maajun.providers.factory import ProviderFactory


def _validate_key(auth: AuthManager, provider: str) -> None:
    """Check a freshly saved key against the provider API, if implemented"""
    if provider not in _implemented_providers():
        console.print(f"[dim]{provider} support is coming soon; key stored for later.[/dim]")
        return

    config = _build_config(auth, provider)
    instance = ProviderFactory.create_provider(
        ProviderType(provider), {"api_key": config.ai.api_key},
    )

    async def validate() -> bool:
        try:
            return await instance.validate_credentials()
        finally:
            await instance.aclose()

    with console.status("[dim]Validating key...[/dim]"):
        valid = asyncio.run(validate())
    if valid:
        console.print("[green]✓ Key validated against the API.[/green]")
    else:
        console.print(
            "[yellow]⚠ Could not validate the key — it may be invalid "
            "or the API is unreachable.[/yellow]"
        )


@app.command()
def login():
    """Set up an API key interactively"""
    auth = AuthManager()
    providers = list(auth.SUPPORTED_PROVIDERS.keys())

    console.print(Panel("[bold]Maajun Login[/bold]", border_style="blue"))

    console.print("\n[bold]Select a provider:[/bold]\n")
    for i, p in enumerate(providers, 1):
        status = "[green]✓[/green]" if auth.has_api_key(p) else "[dim]○[/dim]"
        console.print(f"  {status} [cyan]{i}.[/cyan] {p}")

    while True:
        choice = _input("\n> Choice: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            provider = providers[int(choice) - 1]
            break
        console.print("[red]Invalid choice.[/red]")

    if auth.has_api_key(provider):
        console.print(f"\n[yellow]⚠ {provider} already has a key stored.[/yellow]")
        overwrite = _input("> Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            console.print("[dim]Cancelled.[/dim]")
            return

    console.print(f"\n[bold]Paste your {provider} API key:[/bold]")
    console.print("[dim](input is hidden for security)[/dim]\n")
    key = _secret_input("> API key: ")

    if not key:
        console.print("[red]No key entered. Cancelled.[/red]")
        raise typer.Exit(1)

    auth.set_api_key(provider, key)
    console.print(f"\n[green]✓ {provider} key saved.[/green]")
    _validate_key(auth, provider)
    console.print("\n[dim]Run [bold]maajun chat[/bold] to start chatting.[/dim]")


@app.command()
def provider_list():
    """Show status of all providers"""
    auth = AuthManager()
    implemented = _implemented_providers()

    table = Table(title="AI Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Support")
    table.add_column("API Key")

    for provider_type in ProviderType:
        name = provider_type.value
        support = "[green]Supported[/green]" if name in implemented else "[dim]Coming soon[/dim]"
        key = (
            "[green]✓ Configured[/green]"
            if auth.has_api_key(name)
            else "[yellow]Not configured[/yellow]"
        )
        table.add_row(name, support, key)

    console.print(table)


@app.command()
def config_set_key(
    provider: str = typer.Argument(help="Provider name (e.g. deepseek, openai)"),
    key: str | None = typer.Argument(None, help="API key (omit to be prompted securely)"),
):
    """Store an API key for a provider (prompts if the key is omitted)"""
    if key is None:
        key = _secret_input("API key (input hidden): ")
        if not key:
            console.print("[red]No key entered. Cancelled.[/red]")
            raise typer.Exit(1)
    else:
        console.print(
            "[yellow]⚠ Passing the key as an argument leaves it in shell history. "
            "Prefer omitting it to be prompted, or use [bold]maajun login[/bold].[/yellow]"
        )

    auth = AuthManager()
    try:
        auth.set_api_key(provider, key)
        console.print(f"[green]✓ API key saved for {provider}[/green]")
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e


@app.command()
def config_remove_key(
    provider: str = typer.Argument(help="Provider name"),
):
    """Remove stored API key for a provider"""
    auth = AuthManager()
    auth.clear_provider_key(provider)
    console.print(f"[green]✓ API key removed for {provider}[/green]")


@app.command(name="sign-out")
def sign_out():
    """Clear all stored API keys"""
    auth = AuthManager()
    auth.clear_all()
    console.print("[green]✓ All API keys cleared.[/green]")
