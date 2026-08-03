"""Credential commands: login, provider-list, key management, sign-out."""

from __future__ import annotations

import asyncio

from rich.table import Table

from maajun.auth import AuthManager
from maajun.cli._shared import (
    app,
    build_agent_config,
    console,
    implemented_providers,
)
from maajun.providers.base import ProviderType
from maajun.providers.factory import ProviderFactory


def _validate_key(auth: AuthManager, provider: str) -> None:
    """Check a freshly saved key against the provider API, if implemented"""
    if provider not in implemented_providers():
        console.print(f"[dim]{provider} support is coming soon; key stored for later.[/dim]")
        return

    config = build_agent_config(auth, provider)
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
def provider_list():
    """Show status of all providers"""
    auth = AuthManager()
    implemented = implemented_providers()

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


@app.command(name="sign-out")
def sign_out():
    """Clear all stored API keys"""
    auth = AuthManager()
    auth.clear_all()
    console.print("[green]✓ All API keys cleared.[/green]")
