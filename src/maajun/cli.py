import asyncio
import getpass

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import ProviderError, ProviderType
from maajun.providers.factory import ProviderFactory

app = typer.Typer(invoke_without_command=True)
console = Console()


def _implemented_providers() -> list[str]:
    return [p.value for p in ProviderFactory.get_supported_providers()]


def _configured_providers(auth: AuthManager) -> list[str]:
    """Providers that are both implemented and have a stored key"""
    return [p for p in _implemented_providers() if auth.has_api_key(p)]


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
            "No AI providers configured yet.\n"
            "You need to set up at least one API key to get started.",
            title="Setup Required",
            border_style="yellow",
        ))
        console.print("\n[bold]Quick Setup[/bold]\n")
        console.print("  [cyan]1.[/cyan] Get an API key from your provider:")
        console.print("     • DeepSeek:  https://platform.deepseek.com\n")
        console.print("  [cyan]2.[/cyan] Run [bold]maajun login[/bold] to set up your key.\n")
    else:
        console.print(Panel(
            f"[green]✓ Configured:[/green] {', '.join(configured)}",
            title="Maajun",
            border_style="green",
        ))
        console.print("\n[bold]Commands:[/bold]\n")
        console.print("  [cyan]login[/cyan]              Set up an API key interactively")
        console.print("  [cyan]chat[/cyan]               Start an interactive chat session")
        console.print("  [cyan]provider-list[/cyan]      Show provider status")
        console.print("  [cyan]config-remove-key[/cyan]  Remove a stored API key")
        console.print("  [cyan]sign-out[/cyan]           Clear all stored keys\n")
        console.print("  Run [bold]maajun <command> --help[/bold] for details.\n")


def _build_config(auth: AuthManager, provider: str, thinking: bool = False) -> Config:
    api_key = auth.get_api_key(provider)
    return Config(ai=AIProviderConfig(
        provider=provider, api_key=api_key, thinking_mode=thinking,
    ))


def _pick_provider(configured: list[str]) -> str:
    if len(configured) == 1:
        return configured[0]
    console.print("\n[bold]Select a provider:[/bold]\n")
    for i, p in enumerate(configured, 1):
        console.print(f"  [cyan]{i}.[/cyan] {p}")
    while True:
        choice = console.input("\n> Choice: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(configured):
            return configured[int(choice) - 1]
        console.print("[red]Invalid choice.[/red]")


def _validate_key(auth: AuthManager, provider: str) -> None:
    """Check a freshly saved key against the provider API, if implemented"""
    if provider not in _implemented_providers():
        console.print(f"[dim]{provider} support is coming soon; key stored for later.[/dim]")
        return

    config = _build_config(auth, provider)
    instance = ProviderFactory.create_provider(
        ProviderType(provider), {"api_key": config.ai.api_key},
    )
    with console.status("[dim]Validating key...[/dim]"):
        valid = asyncio.run(instance.validate_credentials())
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
        choice = console.input("\n> Choice: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            provider = providers[int(choice) - 1]
            break
        console.print("[red]Invalid choice.[/red]")

    if auth.has_api_key(provider):
        console.print(f"\n[yellow]⚠ {provider} already has a key stored.[/yellow]")
        overwrite = console.input("> Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            console.print("[dim]Cancelled.[/dim]")
            return

    console.print(f"\n[bold]Paste your {provider} API key:[/bold]")
    console.print("[dim](input is hidden for security)[/dim]\n")
    key = getpass.getpass("> API key: ").strip()

    if not key:
        console.print("[red]No key entered. Cancelled.[/red]")
        raise typer.Exit(1)

    auth.set_api_key(provider, key)
    console.print(f"\n[green]✓ {provider} key saved.[/green]")
    _validate_key(auth, provider)
    console.print("\n[dim]Run [bold]maajun chat[/bold] to start chatting.[/dim]")


@app.command()
def chat(
    provider: str | None = typer.Option(None, "--provider", "-p", help="AI provider to use"),
    thinking: bool = typer.Option(False, "--thinking", help="Enable the provider's reasoning mode"),
):
    """Start an interactive chat session"""
    auth = AuthManager()
    configured = _configured_providers(auth)

    if not configured:
        console.print(
            "[red]No providers configured. Run [bold]maajun login[/bold] to set up a key.[/red]"
        )
        raise typer.Exit(1)

    if provider is None:
        provider = _pick_provider(configured)
    elif provider not in configured:
        console.print(f"[red]Provider '{provider}' is not configured.[/red]")
        console.print(f"[dim]Configured: {', '.join(configured)}[/dim]")
        raise typer.Exit(1)

    from maajun.agent.core import Agent

    agent = Agent(_build_config(auth, provider, thinking))

    console.print(Panel(
        f"[bold]Maajun[/bold]  [dim]({provider})[/dim]\n\n"
        "[dim]Type your message at the > prompt.[/dim]\n"
        "[dim]/clear  /history  /quit[/dim]",
        border_style="blue",
    ))

    try:
        asyncio.run(_chat_loop(agent))
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")


def _response_renderable(thinking: str, content: str) -> Group:
    parts = []
    if thinking.strip():
        parts.append(Panel(
            Text(thinking.strip(), style="dim"),
            title="[dim]Thinking[/dim]",
            border_style="dim",
        ))
    if content:
        parts.append(Markdown(content))
    return Group(*parts)


async def _chat_loop(agent):
    while True:
        try:
            user_input = console.input("\n> ").strip()
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            console.print("[dim]Goodbye![/dim]")
            break

        if user_input == "/clear":
            agent.history.clear()
            console.print("[dim]Session cleared.[/dim]")
            continue

        if user_input == "/history":
            if not agent.history:
                console.print("[dim]No messages yet.[/dim]")
            else:
                for msg in agent.history:
                    if msg["role"] == "user":
                        console.print(f"\n> {msg['content']}")
                    else:
                        console.print()
                        console.print(Markdown(msg["content"]))
            continue

        console.print()
        thinking, content = "", ""
        try:
            with Live(console=console, refresh_per_second=8, vertical_overflow="visible") as live:
                async for kind, text in agent.chat_stream(user_input):
                    if kind == "thinking":
                        thinking += text
                    else:
                        content += text
                    live.update(_response_renderable(thinking, content))
        except ProviderError as e:
            console.print(f"[red]{e}[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error: {e}[/red]")


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
        key = getpass.getpass("API key (input hidden): ").strip()
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


if __name__ == "__main__":
    app()
