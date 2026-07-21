import asyncio
import getpass
import json
import logging
import random
import re
import sys
import time
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.shortcuts import prompt as pt_prompt
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from maajun.agent.core import Agent
from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config, default_config_path
from maajun.daemon import build_daemon
from maajun.providers.base import ProviderError, ProviderType
from maajun.providers.factory import ProviderFactory
from maajun.vcs import GitHubClient, GitHubError

app = typer.Typer(invoke_without_command=True)
console = Console()

PLACEHOLDER_REPO = "owner/name"

STARTER_CONFIG = """\
# Maajun daemon configuration.

[ai]
provider = "deepseek"
# thinking_mode = true

[github]
# Repository the daemon documents errors in and opens PRs against.
repo = "owner/name"
base_branch = "main"
# "suggest": PRs contain only the incident report and suggested fix.
# "fix": the agent may also change code inside its isolated workspace.
mode = "suggest"

[monitor]
# Log files to watch for tracebacks and error lines.
log_files = ["/var/log/myapp/error.log"]
error_pattern = "\\\\b(ERROR|CRITICAL|FATAL)\\\\b"
poll_interval = 30

# GitHub Actions — poll repos for failed workflow runs (optional).
# github_actions_token = "github_pat_..."
# github_actions_repos = ["you/another-repo"]

[daemon]
# Where clones, the incident database, and state live.
# workdir = "~/.local/share/maajun"

# Email notifications when a PR opens or an incident fails (optional).
# [daemon.email]
# smtp_host = "smtp.gmail.com"
# smtp_port = 587                    # 465 for implicit TLS, else STARTTLS
# username = "you@example.com"
# password = ""                      # or set MAAJUN_SMTP_PASSWORD
# from_addr = "you@example.com"
# to_addrs = ["you@example.com"]
"""


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
        console.print("  [cyan]init[/cyan]               Write a starter daemon config")
        console.print("  [cyan]github-login[/cyan]       Store a GitHub token for PRs")
        console.print("  [cyan]watch[/cyan]              Monitor for errors, open PRs")
        console.print("  [cyan]provider-list[/cyan]      Show provider status")
        console.print("  [cyan]config-remove-key[/cyan]  Remove a stored API key")
        console.print("  [cyan]sign-out[/cyan]           Clear all stored keys\n")
        console.print("  Run [bold]maajun <command> --help[/bold] for details.\n")


def _build_config(auth: AuthManager, provider: str, thinking: bool = False) -> Config:
    api_key = auth.get_api_key(provider)
    return Config(ai=AIProviderConfig(
        provider=provider, api_key=api_key, thinking_mode=thinking,
    ))


def _input(text: str) -> str:
    """Paste-safe line prompt.

    prompt_toolkit enables bracketed paste, so a multi-line paste lands in
    the prompt buffer as one editable input instead of submitting line by
    line and leaking the rest to the shell after exit. Unbound key combos
    are ignored rather than inserting escape codes. Falls back to plain
    input when stdin is not a TTY (tests, pipes).
    """
    if sys.stdin.isatty():
        return pt_prompt(text)
    return console.input(text)


def _secret_input(text: str) -> str:
    """Paste-safe hidden prompt for keys and tokens (echoes '*').

    Rejects multi-line pastes: no token or API key contains a newline, so
    one almost always means the wrong thing was pasted — better to re-ask
    than to send the first line to an API and dump the rest.
    """
    while True:
        if sys.stdin.isatty():
            value = pt_prompt(text, is_password=True).strip()
        else:
            value = getpass.getpass(text).strip()
        if "\n" not in value:
            return value
        console.print(
            "[yellow]⚠ That paste spans multiple lines — it doesn't look like "
            "a key or token. Check your clipboard and try again.[/yellow]"
        )


def _pick_provider(configured: list[str]) -> str:
    if len(configured) == 1:
        return configured[0]
    console.print("\n[bold]Select a provider:[/bold]\n")
    for i, p in enumerate(configured, 1):
        console.print(f"  [cyan]{i}.[/cyan] {p}")
    while True:
        choice = _input("\n> Choice: ").strip()
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


def _format_tool_args(args: dict) -> str:
    text = json.dumps(args, indent=2)
    if len(text) > 500:
        text = text[:500] + "\n... (truncated)"
    return text


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


    # Holds the active Live display so the approval prompt can pause it.
    live_holder: dict = {"live": None}

    # prompt_toolkit reads keys in raw mode: a paste (Ctrl+Shift+V) arrives as
    # one bracketed-paste event and is inserted literally — a multi-line paste
    # stays one message instead of submitting line by line — and unbound key
    # combinations are ignored instead of leaking escape codes into the input.
    prompt_session = PromptSession() if sys.stdin.isatty() else None

    async def approve_always(name: str, args: dict) -> bool:
        return True

    async def approve_interactively(name: str, args: dict) -> bool:
        live = live_holder["live"]
        if live:
            live.stop()
        console.print()
        console.print(Panel(
            f"[bold]{name}[/bold]\n{_format_tool_args(args)}",
            title="[yellow]Tool needs permission[/yellow]",
            border_style="yellow",
        ))
        try:
            answer = (await _prompt(prompt_session, "> Allow this call? (y/N): ")).strip().lower()
        except EOFError:
            answer = ""
        if live:
            live.start()
        return answer == "y"

    agent = Agent(
        _build_config(auth, provider, thinking),
        approve=approve_always if auto_approve else approve_interactively,
    )

    permission_note = (
        "[dim]Tools run automatically (--auto-approve).[/dim]"
        if auto_approve
        else "[dim]You'll be asked before commands run or files change.[/dim]"
    )
    console.print(Panel(
        f"[bold]Maajun[/bold]  [dim]({provider})[/dim]\n\n"
        "[dim]Type your message at the > prompt.[/dim]\n"
        f"{permission_note}\n"
        "[dim]/clear  /history  /quit[/dim]",
        border_style="blue",
    ))

    try:
        asyncio.run(_chat_loop(agent, live_holder, prompt_session))
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")


async def _prompt(session: PromptSession | None, text: str) -> str:
    """Read a line of input, via prompt_toolkit when stdin is a TTY."""
    if session is not None:
        return await session.prompt_async(text)
    return console.input(text)


STREAM_TAIL_LINES = 10

STATUS_WORDS = [
    "Thinking", "Reasoning", "Pondering", "Cogitating", "Mulling",
    "Musing", "Percolating", "Brewing", "Ruminating", "Deliberating",
    "Puzzling", "Noodling", "Marinating", "Untangling",
    "Connecting dots", "Weighing options", "Piecing it together",
]
STATUS_WORD_SECONDS = 2.5


class _StatusSpinner:
    """Animated 'Thinking… (3s)' line shown instead of the reasoning text.

    Re-renders on every Live refresh, so the spinner, the elapsed timer, and
    the periodic word rotation all keep moving even while the stream stalls.
    """

    def __init__(self):
        self._started = time.monotonic()
        self._word = random.choice(STATUS_WORDS)
        self._word_at = self._started
        self._spinner = Spinner("dots", style="dim")

    def __rich_console__(self, console, options):
        now = time.monotonic()
        if now - self._word_at >= STATUS_WORD_SECONDS:
            self._word = random.choice([w for w in STATUS_WORDS if w != self._word])
            self._word_at = now
        elapsed = int(now - self._started)
        self._spinner.update(text=Text(f"{self._word}… ({elapsed}s)", style="dim"))
        yield self._spinner


def _tail(text: str, limit: int = STREAM_TAIL_LINES) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-limit:])


def _stream_renderable(status: _StatusSpinner, content: str) -> Group:
    """Bounded view of the in-flight response.

    The live region must stay shorter than the terminal — Rich's Live cannot
    rewrite lines that have scrolled off screen, so an unbounded renderable
    leaves a stale copy behind on every refresh. Only the tail of the content
    is shown while streaming; the full response is printed once at the end.
    """
    parts = []
    if content.strip():
        parts.append(Text(_tail(content)))
    parts.append(status)
    return Group(*parts)


async def _chat_loop(agent, live_holder=None, prompt_session=None):
    live_holder = live_holder if live_holder is not None else {"live": None}
    while True:
        console.print()
        try:
            user_input = (await _prompt(prompt_session, "> ")).strip()
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            console.print("[dim]Goodbye![/dim]")
            break

        if user_input == "/clear":
            agent.clear_history()
            console.print("[dim]Session cleared.[/dim]")
            continue

        if user_input == "/history":
            if not agent.history:
                console.print("[dim]No messages yet.[/dim]")
            else:
                for msg in agent.history:
                    if msg["role"] == "user":
                        console.print(f"\n> {msg['content']}")
                    elif msg["role"] == "assistant" and msg.get("content"):
                        console.print()
                        console.print(
                            msg["content"], markup=False, highlight=False, soft_wrap=True
                        )
            continue

        console.print()
        content = ""
        error = None
        status = _StatusSpinner()
        try:
            # transient=True: the bounded streaming view is erased on exit and
            # replaced by a single full print of the response below.
            with Live(console=console, refresh_per_second=8, transient=True) as live:
                live_holder["live"] = live
                live.update(_stream_renderable(status, content))
                try:
                    async for kind, text in agent.chat_stream(user_input):
                        if kind == "tool":
                            # Printed while Live is active, so it lands above
                            # the live region and stays there permanently.
                            console.print(Text(text, style="dim"))
                        elif kind == "content":
                            content += text
                            live.update(_stream_renderable(status, content))
                        # "thinking" chunks are deliberately not shown — the
                        # animated status stands in for the reasoning text.
                finally:
                    live_holder["live"] = None
        except ProviderError as e:
            error = str(e)
        except Exception as e:
            error = f"Unexpected error: {e}"

        if content:
            # Plain text, unwrapped: what you copy is exactly what the model
            # wrote, with no table borders, padding, or re-flowed lines.
            console.print(content, markup=False, highlight=False, soft_wrap=True)
        if error:
            console.print(f"[red]{error}[/red]")


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


@app.command()
def init(
    path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Write a starter config file for the monitoring daemon"""
    path = path or default_config_path()
    if path.exists():
        console.print(f"[yellow]⚠ {path} already exists.[/yellow]")
        overwrite = _input("> Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            console.print("[dim]Cancelled.[/dim]")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER_CONFIG)
    console.print(f"[green]✓ Wrote {path}[/green]")
    console.print(
        "\n[bold]Next steps:[/bold]\n"
        "  1. Edit the config: set [cyan]monitor.log_files[/cyan] (or GitHub Actions)\n"
        "  2. Run [bold]maajun github-login[/bold] to set the repo and store a token\n"
        "  3. Run [bold]maajun watch[/bold] to start monitoring\n"
    )


def _save_repo_to_config(path: Path, repo: str) -> None:
    """Set github.repo in the config file, creating the file if needed.

    Edits the existing file in place (one line) so comments survive.
    """
    if path.exists():
        text = path.read_text()
        new, n = re.subn(
            r'(?m)^(\s*)repo\s*=\s*"[^"]*"',
            rf'\g<1>repo = "{repo}"',
            text,
            count=1,
        )
        if n == 0:
            if "[github]" in text:
                new = text.replace("[github]", f'[github]\nrepo = "{repo}"', 1)
            else:
                new = f'{text.rstrip()}\n\n[github]\nrepo = "{repo}"\n'
        path.write_text(new)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            STARTER_CONFIG.replace(f'repo = "{PLACEHOLDER_REPO}"', f'repo = "{repo}"')
        )


@app.command(name="github-login")
def github_login(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
):
    """Set the target repository and store a GitHub token, in one step.

    Use a fine-grained personal access token scoped to the target repo with
    Contents: read/write and Pull requests: read/write permissions.
    """
    console.print(Panel(
        "[bold]GitHub Login[/bold]\n\n"
        "Create a fine-grained personal access token at\n"
        "[cyan]https://github.com/settings/personal-access-tokens[/cyan]\n\n"
        "Scope it to the repository maajun will open PRs on, with:\n"
        "  • Contents: [bold]read and write[/bold]\n"
        "  • Pull requests: [bold]read and write[/bold]",
        border_style="blue",
    ))

    config = Config.load(config_path)
    current = config.github.repo if config.github.repo != PLACEHOLDER_REPO else ""

    prompt = f"\n> Repository (owner/name) [{current}]: " if current else \
        "\n> Repository (owner/name): "
    repo = _input(prompt).strip() or current
    if not repo:
        console.print("[red]No repository entered. Cancelled.[/red]")
        raise typer.Exit(1)
    if repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
        console.print(f'[red]✗ "{repo}" is not in owner/name form.[/red]')
        raise typer.Exit(1)

    token = _secret_input("> GitHub token (input hidden): ")
    if not token:
        console.print("[red]No token entered. Cancelled.[/red]")
        raise typer.Exit(1)

    client = GitHubClient(token)

    async def validate() -> tuple[str, bool]:
        login = await client.validate_token()
        push = await client.can_push(repo)
        return login, push

    try:
        with console.status("[dim]Validating token...[/dim]"):
            login, push = asyncio.run(validate())
    except GitHubError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]✓ Authenticated as {login}.[/green]")
    if push:
        console.print(f"[green]✓ Token can push to {repo}.[/green]")
    else:
        console.print(
            f"[yellow]⚠ Token cannot push to {repo} — "
            "the daemon will fail to create branches. Check the token's "
            "repository access and Contents permission.[/yellow]"
        )

    if repo != config.github.repo:
        path = config_path or default_config_path()
        _save_repo_to_config(path, repo)
        console.print(f"[green]✓ Saved github.repo = {repo} in {path}.[/green]")

    auth = AuthManager()
    try:
        auth.set_github_token(token)
        console.print("[green]✓ Token stored.[/green]")
    except RuntimeError as e:
        console.print(
            f"[yellow]⚠ Could not store in keyring: {e}\n"
            "On a server, export it instead: [bold]GITHUB_TOKEN=...[/bold][/yellow]"
        )


@app.command()
def watch(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Config file location"),
    once: bool = typer.Option(False, "--once", help="Run a single poll cycle and exit"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Analyze errors but skip git/PR operations"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
):
    """Monitor configured error sources; document each new error in a PR"""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.load(config_path)
    try:
        daemon = build_daemon(config)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e

    console.print(Panel(
        f"[bold]Maajun watch[/bold]\n\n"
        f"Repo:     [cyan]{config.github.repo}[/cyan] "
        f"(base: {config.github.base_branch})\n"
        f"Mode:     [cyan]{config.github.mode}[/cyan]\n"
        f"Monitors: {', '.join(m.name for m in daemon.monitors)}\n"
        f"Interval: {config.monitor.poll_interval}s"
        + ("\n[yellow]Dry run — no branches/PRs will be created[/yellow]" if dry_run else ""),
        border_style="blue",
    ))

    try:
        asyncio.run(daemon.run(once=once, dry_run=dry_run))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@app.command(name="sign-out")
def sign_out():
    """Clear all stored API keys"""
    auth = AuthManager()
    auth.clear_all()
    console.print("[green]✓ All API keys cleared.[/green]")


if __name__ == "__main__":
    app()
