from __future__ import annotations

import getpass
import sys

import typer
from prompt_toolkit.shortcuts import prompt as pt_prompt
from rich.console import Console
from rich.markup import escape

from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config, ConfigError, RepoConfig
from maajun.providers.factory import ProviderFactory

app = typer.Typer(invoke_without_command=True)
console = Console()


def load_config(path) -> Config:
    try:
        return Config.load(path)
    except ConfigError as e:
        console.print(f"[red]✗ {escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    except (OSError, ValueError) as e:
        # tomllib raises TOMLDecodeError (a ValueError) on a malformed file.
        console.print(f"[red]✗ Could not read the config: {escape(str(e))}[/red]")
        raise typer.Exit(1) from e


class Asker:
    """Prompts that fall back to defaults when running non-interactively."""

    def __init__(self, interactive: bool):
        self.interactive = interactive

    def text(self, prompt: str, default: str = "") -> str:
        if not self.interactive:
            return default
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        return prompt_line(f"> {shown}").strip() or default

    def secret(self, prompt: str) -> str:
        if not self.interactive:
            return ""
        return prompt_secret(f"> {prompt}: ")

    def confirm(self, prompt: str, default: bool = False) -> bool:
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        answer = prompt_line(f"> {prompt} ({hint}): ").strip().lower()
        if not answer:
            return default
        return answer.startswith("y")


def split_list(value: str | None) -> list[str] | None:
    """A comma-separated flag as a list. None stays None: "leave as is"."""
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def implemented_providers() -> list[str]:
    return [p.value for p in ProviderFactory.get_supported_providers()]


def provider_choices() -> list[str]:
    """The provider names as offered, with the free one marked."""
    return [
        f"{name} (free)" if ProviderFactory.is_free(name) else name
        for name in implemented_providers()
    ]


def configured_providers(auth: AuthManager) -> list[str]:
    return [p for p in implemented_providers() if auth.has_api_key(p)]


def build_agent_config(auth: AuthManager, provider: str, thinking: bool = False) -> Config:
    api_key = auth.get_api_key(provider)
    return Config(ai=AIProviderConfig(
        provider=provider, api_key=api_key, thinking_mode=thinking,
    ))


def prompt_line(text: str) -> str:
    """Paste-safe line prompt.

    prompt_toolkit enables bracketed paste, so a multi-line paste lands in
    the prompt buffer as one editable input instead of submitting line by
    line and leaking the rest to the shell after exit. Unbound key combos
    are ignored rather than inserting escape codes. Falls back to plain
    input when stdin is not a TTY (tests, pipes).

    markup=False on the fallback: prompts show defaults as "[main]", which
    Rich would otherwise parse as a style tag and render as nothing.
    """
    if sys.stdin.isatty():
        return pt_prompt(text)
    return console.input(text, markup=False)


def prompt_secret(text: str) -> str:
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


def prompt_mode(current: str = "suggest") -> str:
    """Prompt for suggest/fix mode, defaulting to `current`."""
    console.print("\n[bold]Mode[/bold]")
    console.print(
        "  [cyan]1.[/cyan] suggest — Github issues contain only the incident "
        "report and suggested fix"
    )
    console.print(
        "  [cyan]2.[/cyan] fix — the agent may also change code inside its workspace"
    )
    default = "1" if current != "fix" else "2"
    choice = prompt_line(f"> Mode (1/2) [{default}]: ").strip() or default
    if choice == "2":
        return "fix"
    if choice == "1":
        return "suggest"
    console.print(f"[yellow]⚠ Invalid choice. Using {current}.[/yellow]")
    return current


def pick_repo(repos: list[RepoConfig]) -> RepoConfig:
    console.print("\n[bold]Select a repository:[/bold]\n")
    for i, rc in enumerate(repos, 1):
        console.print(f"  [cyan]{i}.[/cyan] {rc.repo} [dim](mode: {rc.mode})[/dim]")
    while True:
        choice = prompt_line("\n> Choice: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(repos):
            return repos[int(choice) - 1]
        console.print("[red]Invalid choice.[/red]")
