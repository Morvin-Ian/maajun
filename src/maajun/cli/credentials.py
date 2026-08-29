from __future__ import annotations

from rich.table import Table

from maajun.auth import AuthManager
from maajun.cli.shared import (
    app,
    console,
    implemented_providers,
)
from maajun.providers.base import ProviderType


@app.command()
def provider_list():
    """Show which AI providers are supported and which have a key stored."""
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
    """Clear every stored credential — provider API keys and the GitHub token."""
    auth = AuthManager()
    auth.clear_all()
    # clear_all() drops the GitHub token too, so say so.
    console.print(
        "[green]✓ Cleared all provider API keys and the GitHub token.[/green]"
    )
