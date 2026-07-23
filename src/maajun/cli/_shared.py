"""Shared Typer app, console, and interactive helpers for the CLI package.

Every command module registers on the single `app` defined here, so the CLI
stays flat (`maajun login`, `maajun watch`, …) even though the commands live
in separate modules.
"""

from __future__ import annotations

import getpass
import sys

import typer
from prompt_toolkit.shortcuts import prompt as pt_prompt
from rich.console import Console

from maajun.auth import AuthManager
from maajun.config import AIProviderConfig, Config, RepoConfig
from maajun.providers.factory import ProviderFactory

app = typer.Typer(invoke_without_command=True)
console = Console()


def _implemented_providers() -> list[str]:
    return [p.value for p in ProviderFactory.get_supported_providers()]


def _configured_providers(auth: AuthManager) -> list[str]:
    """Providers that are both implemented and have a stored key"""
    return [p for p in _implemented_providers() if auth.has_api_key(p)]


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


def _prompt_mode(current: str = "suggest") -> str:
    """Prompt for suggest/fix mode, defaulting to `current`. Shared by init
    and github-login so the wording stays in one place."""
    console.print("\n[bold]Mode[/bold]")
    console.print(
        "  [cyan]1.[/cyan] suggest — PRs contain only the incident report and suggested fix"
    )
    console.print(
        "  [cyan]2.[/cyan] fix — the agent may also change code inside its workspace"
    )
    default = "1" if current != "fix" else "2"
    choice = _input(f"> Mode (1/2) [{default}]: ").strip() or default
    if choice == "2":
        return "fix"
    if choice == "1":
        return "suggest"
    console.print(f"[yellow]⚠ Invalid choice. Using {current}.[/yellow]")
    return current


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


def _pick_repo(repos: list[RepoConfig]) -> RepoConfig:
    console.print("\n[bold]Select a repository:[/bold]\n")
    for i, rc in enumerate(repos, 1):
        console.print(f"  [cyan]{i}.[/cyan] {rc.repo} [dim](mode: {rc.mode})[/dim]")
    while True:
        choice = _input("\n> Choice: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(repos):
            return repos[int(choice) - 1]
        console.print("[red]Invalid choice.[/red]")
