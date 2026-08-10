
from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.auth import AuthManager
from maajun.cli._shared import (
    app,
    configured_providers,
    console,
    implemented_providers,
    load_config,
    prompt_line,
    prompt_mode,
    prompt_secret,
)
from maajun.cli.status_checks import build_status, gather_github
from maajun.config import Config, RepoConfig, default_config_path
from maajun.providers.base import ProviderType
from maajun.providers.factory import ProviderFactory
from maajun.utils import is_valid_repo
from maajun.vcs import GitHubClient, GitHubError

PROVIDER_SIGNUP_URLS = {
    "deepseek": "https://platform.deepseek.com",
    "openai": "https://platform.openai.com/api-keys",
}

GITHUB_TOKEN_URL = "https://github.com/settings/personal-access-tokens"

_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def detect_repo_from_git(directory: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory or Path.cwd()), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    match = _GITHUB_REMOTE_RE.search(result.stdout.strip())
    return match.group(1) if match else None

class _Asker:
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


def _step(number: int, total: int, title: str, optional: bool = False) -> None:
    tag = " [dim](optional — press Enter to skip)[/dim]" if optional else ""
    console.print(f"\n[bold cyan]({number}/{total})[/bold cyan] [bold]{title}[/bold]{tag}")


def _validate_api_key(provider: str, key: str, base_url: str | None = None) -> bool:
    instance = ProviderFactory.create_provider(
        ProviderType(provider), {"api_key": key, "base_url": base_url}
    )

    async def check() -> bool:
        try:
            return await instance.validate_credentials()
        finally:
            await instance.aclose()

    with console.status("[dim]Validating key...[/dim]"):
        return asyncio.run(check())


def _setup_provider(
    auth: AuthManager,
    ask: _Asker,
    requested: str | None,
    reconfigure: bool,
    base_url: str | None = None,
) -> str:
    implemented = implemented_providers()
    configured = configured_providers(auth)

    provider = requested or (configured[0] if configured else implemented[0])
    if requested and requested not in implemented:
        console.print(
            f"[red]✗ Unknown provider {requested!r}. "
            f"Choose one of: {', '.join(implemented)}[/red]"
        )
        raise typer.Exit(1)
    if len(implemented) > 1 and not requested:
        provider = ask.text(f"AI provider ({'/'.join(implemented)})", provider)
        if provider not in implemented:
            console.print(f"[red]✗ Unknown provider {provider!r}.[/red]")
            raise typer.Exit(1)

    if auth.has_api_key(provider) and not reconfigure:
        console.print(f"  [green]✓[/green] {provider} API key already stored")
        return provider

    signup = PROVIDER_SIGNUP_URLS.get(provider)
    if signup:
        console.print(f"  [dim]Get a key at {signup}[/dim]")
    if not ask.interactive:
        # Keys live only in the keyring, and a key cannot be prompted for
        # here — so an unattended run can configure repos and monitors on a
        # machine that is already set up, but cannot bootstrap a fresh one.
        console.print(
            f"[red]✗ No API key for {provider} in the keyring. Run "
            "'maajun setup' interactively once to store one.[/red]"
        )
        raise typer.Exit(1)

    for attempt in range(3):
        key = ask.secret(f"{provider} API key (input hidden)")
        if not key:
            console.print("[red]✗ An API key is required — nothing else works "
                          "without it.[/red]")
            raise typer.Exit(1)
        if _validate_api_key(provider, key, base_url):
            _store_api_key(auth, provider, key)
            console.print("  [green]✓[/green] Key validated and stored")
            return provider
        console.print("  [yellow]⚠ The API rejected that key.[/yellow]")
        if attempt < 2 and ask.confirm("Try again?", default=True):
            continue
        if ask.confirm("Store it anyway?", default=False):
            _store_api_key(auth, provider, key)
            return provider
        raise typer.Exit(1)
    return provider


def _store_api_key(auth: AuthManager, provider: str, key: str) -> None:
    try:
        auth.set_api_key(provider, key)
    except RuntimeError as e:
        # The keyring is the only store, so this is fatal rather than a
        # fallback — say so instead of implying the key was kept.
        console.print(
            f"  [red]✗ Could not store the key: {e}[/red]"
        )
        raise typer.Exit(1) from e


def _setup_github(
    auth: AuthManager,
    config: Config,
    ask: _Asker,
    *,
    requested_repo: str | None,
    base_branch: str | None,
    mode: str | None,
    test_command: str | None,
    reconfigure: bool,
) -> None:
    existing = config.github.get_all_repos()
    current = existing[0].repo if existing else ""
    # Only ever a prefilled suggestion: silently adopting the surrounding
    # checkout would be a surprising side effect of a --non-interactive run.
    detected = detect_repo_from_git() if ask.interactive else None
    if detected and not current:
        console.print(f"  [dim]Detected from git remote: {detected}[/dim]")

    repo = requested_repo or ask.text(
        "Repository to open PRs on (owner/name)", current or detected or ""
    )
    if not repo:
        console.print(
            "  [dim]Skipped — maajun will analyze errors and write reports "
            "to disk instead of opening PRs.[/dim]"
        )
        return
    if not is_valid_repo(repo):
        console.print(
            f'  [yellow]⚠ "{repo}" is not in owner/name form — skipping GitHub '
            "setup. Re-run 'maajun setup' to fix it.[/yellow]"
        )
        return

    branch = base_branch or ask.text(
        "Base branch", existing[0].base_branch if existing else "main"
    )
    resolved_mode = mode or (
        prompt_mode(existing[0].mode if existing else "suggest")
        if ask.interactive else (existing[0].mode if existing else "suggest")
    )

    # Only fix mode produces a diff, so only fix mode has anything to verify.
    current_test_command = existing[0].test_command if existing else ""
    resolved_test_command = current_test_command
    if resolved_mode == "fix":
        if test_command is not None:
            resolved_test_command = test_command
        elif ask.interactive:
            console.print(
                "  [dim]Fix mode edits code. A test command lets maajun verify "
                "the fix and put the result in the PR.[/dim]"
            )
            resolved_test_command = ask.text(
                "Test command (Enter to skip)", current_test_command
            )
        if not resolved_test_command:
            console.print(
                "  [yellow]⚠ No test command — fix-mode PRs will be marked "
                "unverified.[/yellow]"
            )

    # Update the entry in place when this repo is already configured, rather
    # than replacing it — setup never asks about log_files, so rebuilding the
    # entry from these three answers would drop the ones already set.
    entry = next((rc for rc in config.github.repos if rc.repo == repo), None)
    if entry is not None:
        entry.base_branch = branch
        entry.mode = resolved_mode
        entry.test_command = resolved_test_command
    else:
        config.add_repo(RepoConfig(
            repo=repo, base_branch=branch, mode=resolved_mode,
            test_command=resolved_test_command,
        ))

    _setup_github_token(auth, ask, repo, reconfigure=reconfigure)


def _setup_github_token(
    auth: AuthManager, ask: _Asker, repo: str, *, reconfigure: bool
) -> None:
    if auth.has_github_token() and not reconfigure:
        console.print("  [green]✓[/green] GitHub token already stored")
        _report_push_access(auth, repo)
        return

    console.print(
        f"  [dim]Create a fine-grained token at {GITHUB_TOKEN_URL}\n"
        f"    Scope it to {repo} with Contents: read/write and "
        "Pull requests: read/write.[/dim]"
    )
    token = ask.secret("GitHub token (input hidden, Enter to skip)")
    if not token:
        console.print(
            "  [yellow]⚠ No token — PRs will fail until you re-run "
            "'maajun setup' and provide one.[/yellow]"
        )
        return
    try:
        auth.set_github_token(token)
        console.print("  [green]✓[/green] Token stored")
    except RuntimeError as e:
        console.print(f"  [red]✗ Could not store the token: {e}[/red]")
        return
    _report_push_access(auth, repo)


def _report_push_access(auth: AuthManager, repo: str) -> None:
    """Warn early if the token cannot push — the daemon would fail much later."""
    token = auth.get_github_token()
    if not token:
        return
    client = GitHubClient(token)
    try:
        with console.status("[dim]Checking repository access...[/dim]"):
            login, pushable = asyncio.run(
                gather_github(client, [RepoConfig(repo=repo)])
            )
    except GitHubError as e:
        console.print(f"  [yellow]⚠ Could not reach GitHub: {e}[/yellow]")
        return
    if login is None:
        console.print("  [yellow]⚠ GitHub rejected the token.[/yellow]")
        return
    console.print(f"  [green]✓[/green] Authenticated as {login}")
    if not pushable.get(repo):
        console.print(
            f"  [yellow]⚠ The token cannot push to {repo}. Check its "
            "repository access and Contents permission.[/yellow]"
        )


def _setup_error_sources(
    auth: AuthManager,
    config: Config,
    ask: _Asker,
    *,
    log_paths: str | None,
    github_actions: bool | None,
) -> None:
    logs = log_paths if log_paths is not None else ask.text(
        "Log files to watch (comma-separated)",
        ",".join(config.monitor.log_files),
    )
    config.monitor.log_files = [path.strip() for path in logs.split(",") if path.strip()]
    for path in config.monitor.log_files:
        if Path(path).expanduser().exists():
            console.print(f"  [green]✓[/green] {path}")
        else:
            # Not a problem: the app may only create its error log on first use.
            console.print(f"  [yellow]⚠[/yellow] {path} [dim](not found yet)[/dim]")

    configured_repos = config.github.get_all_repos()
    want_actions = (
        github_actions if github_actions is not None
        else (
            bool(configured_repos)
            and ask.confirm("Watch GitHub Actions for failed runs?")
        )
    )
    if want_actions:
        repo_names = [repo_config.repo for repo_config in configured_repos]
        if not repo_names:
            console.print("  [yellow]⚠ GitHub Actions needs a configured repo — "
                          "skipped.[/yellow]")
        elif not auth.has_github_token():
            console.print("  [yellow]⚠ GitHub Actions needs a GitHub token — "
                          "skipped.[/yellow]")
        else:
            # Only the repo list is stored. The token is read from the keyring
            # or the environment at run time — writing it here would put a
            # secret in a plaintext config file.
            config.monitor.github_actions_repos = repo_names
            console.print(
                f"  [green]✓[/green] Watching Actions on {', '.join(repo_names)}"
            )


@app.command()
def setup(
    config_path: Path | None = typer.Option(
        None, "--config", "-c", help="Config file location"
    ),
    provider: str | None = typer.Option(None, "--provider", help="AI provider to use"),
    repo: str | None = typer.Option(
        None, "--repo", help="Repository to open PRs on (owner/name)"
    ),
    base_branch: str | None = typer.Option(
        None, "--base-branch", "-b", help="Branch to open PRs against"
    ),
    mode: str | None = typer.Option(None, "--mode", "-m", help="'suggest' or 'fix'"),
    test_command: str | None = typer.Option(
        None, "--test-command",
        help="Command that verifies a fix-mode edit, e.g. 'pytest -q'",
    ),
    logs: str | None = typer.Option(
        None, "--logs", "-l", help="Comma-separated log files to watch"
    ),
    github_actions: bool | None = typer.Option(
        None, "--github-actions/--no-github-actions",
        help="Watch the configured repos for failed workflow runs",
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive",
        help="Never prompt; take everything from flags and the environment",
    ),
    reconfigure: bool = typer.Option(
        False, "--reconfigure",
        help="Ask again for credentials that are already stored",
    ),
):
    """Configure maajun: the provider key, GitHub, and the error sources."""
    path = config_path or default_config_path()
    ask = _Asker(interactive=not non_interactive)
    auth = AuthManager()
    config = load_config(path)

    console.print(Panel(
        "[bold]Maajun setup[/bold]\n\n"
        "Only the API key is required — press Enter to skip anything else.",
        border_style="blue",
    ))

    total = 3
    _step(1, total, "AI provider")
    config.ai.provider = _setup_provider(
        auth, ask, provider, reconfigure, config.ai.base_url
    )

    _step(2, total, "GitHub", optional=True)
    _setup_github(
        auth, config, ask,
        requested_repo=repo, base_branch=base_branch, mode=mode,
        test_command=test_command, reconfigure=reconfigure,
    )

    _step(3, total, "Error sources", optional=True)
    _setup_error_sources(
        auth, config, ask,
        log_paths=logs, github_actions=github_actions,
    )

    config.save(path)
    console.print(f"\n[green]✓ Wrote {path}[/green]")
    _print_summary(config, auth)


def _print_summary(config: Config, auth: AuthManager) -> None:
    repos = config.github.get_all_repos()
    has_token = auth.has_github_token()
    sections, ok = build_status(
        config,
        provider=config.ai.provider,
        has_key=auth.has_api_key(config.ai.provider),
        has_token=has_token,
        repos=repos,
        network=None,
    )
    console.print("\n[bold]Status[/bold]")
    for section in sections:
        for check in section.checks:
            if check.ok:
                mark = "[green]✓[/green]"
            elif check.warn:
                mark = "[yellow]⚠[/yellow]"
            else:
                mark = "[red]✗[/red]"
            detail = f" [dim]{check.detail}[/dim]" if check.detail else ""
            console.print(f"  {mark} {check.label}{detail}")

    if not ok:
        console.print(
            "\n[yellow]Fix the ✗ items above, then run "
            "[bold]maajun status[/bold] to re-check.[/yellow]"
        )
        return
    if repos:
        console.print("\n[bold]You're ready.[/bold] Next:")
        console.print("  [cyan]maajun watch --dry-run[/cyan]  "
                      "[dim]analyze without opening PRs[/dim]")
        console.print("  [cyan]maajun watch[/cyan]            "
                      "[dim]start monitoring for real[/dim]")
    else:
        console.print("\n[bold]You're ready.[/bold] Next:")
        console.print("  [cyan]maajun watch[/cyan]  "
                      "[dim]analyze errors into local reports[/dim]")
        console.print("  [dim]Run 'maajun setup' again to connect GitHub "
                      "and open PRs instead.[/dim]")
