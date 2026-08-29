
from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import typer
from rich.panel import Panel

from maajun.auth import (
    AuthManager,
    credentials_file,
    install_backend_command,
    keyring_works,
)
from maajun.cli.deployment import record_deployment
from maajun.cli.github_auth import authenticate, report_push_access
from maajun.cli.shared import (
    Asker,
    app,
    configured_providers,
    console,
    implemented_providers,
    load_config,
    prompt_mode,
    provider_choices,
)
from maajun.cli.status_checks import build_status
from maajun.config import Config, RepoConfig, default_config_path
from maajun.daemon import service
from maajun.discovery import probe_source
from maajun.providers.base import ProviderType
from maajun.providers.factory import ProviderFactory
from maajun.utils import is_valid_repo, qualify
from maajun.vcs.gh import account_login, gh_account, ssh_works

PROVIDER_SIGNUP_URLS = {
    "ox-alpha": "https://openrouter.ai/settings/keys",
    "deepseek": "https://platform.deepseek.com",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
}

GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def detect_repo_from_git(directory: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory or Path.cwd()), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    match = GITHUB_REMOTE_RE.search(result.stdout.strip())
    return match.group(1) if match else None

def step(number: int, total: int, title: str, optional: bool = False) -> None:
    tag = " [dim](optional — press Enter to skip)[/dim]" if optional else ""
    console.print(f"\n[bold cyan]({number}/{total})[/bold cyan] [bold]{title}[/bold]{tag}")


def validate_api_key(provider: str, key: str, base_url: str | None = None) -> bool:
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


def setup_provider(
    auth: AuthManager,
    ask: Asker,
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
        provider = ask.text(f"AI provider ({'/'.join(provider_choices())})", provider)
        if provider not in implemented:
            console.print(f"[red]✗ Unknown provider {provider!r}.[/red]")
            raise typer.Exit(1)

    if auth.has_api_key(provider) and not reconfigure:
        console.print(f"  [green]✓[/green] {provider} API key already stored")
        return provider

    say_where_credentials_go()

    if ProviderFactory.is_free(provider):
        console.print(
            "  [dim]This model is free — the key is only how OpenRouter "
            "identifies you.[/dim]"
        )
    signup = PROVIDER_SIGNUP_URLS.get(provider)
    if signup:
        console.print(f"  [dim]Get a key at {signup}[/dim]")
    if not ask.interactive:
        # No prompt means no new key, so this configures an already-set-up
        # machine but cannot bootstrap a fresh one.
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
        if validate_api_key(provider, key, base_url):
            store_api_key(auth, provider, key)
            console.print("  [green]✓[/green] Key validated and stored")
            return provider
        console.print("  [yellow]⚠ The API rejected that key.[/yellow]")
        if attempt < 2 and ask.confirm("Try again?", default=True):
            continue
        if ask.confirm("Store it anyway?", default=False):
            store_api_key(auth, provider, key)
            return provider
        raise typer.Exit(1)
    return provider


def say_where_credentials_go() -> None:
    """Mention the file, once, when there is no keyring to use instead.

    A statement, not a question: there is one sensible answer on a server,
    and asking it of everyone who installs maajun there is friction for
    nothing. The alternative is named for anyone who wants it.
    """
    if keyring_works():
        return
    console.print(
        f"  [dim]No keyring on this machine, so credentials go in "
        f"{credentials_file()} (chmod 600).\n"
        f"    To use a keyring instead: {install_backend_command()}[/dim]"
    )


def store_api_key(auth: AuthManager, provider: str, key: str) -> None:
    try:
        auth.set_api_key(provider, key)
    except RuntimeError as e:
        console.print(f"  [red]✗ Could not store the key: {e}[/red]")
        raise typer.Exit(1) from e


def setup_github(
    auth: AuthManager,
    config: Config,
    ask: Asker,
    *,
    requested_repo: str | None,
    base_branch: str | None,
    mode: str | None,
    test_command: str | None,
    reconfigure: bool,
) -> None:
    existing = config.github.repos
    current = existing[0].repo if existing else ""
    # A suggestion only; adopting the surrounding checkout silently would
    # be a surprising side effect.
    detected = detect_repo_from_git() if ask.interactive else None
    if detected and not current:
        console.print(f"  [dim]Detected from git remote: {detected}[/dim]")

    owner = account_login(auth.get_github_token())
    hint = "Repository to open PRs on" + (f" (name, or {owner}/name)" if owner else " (owner/name)")
    repo = requested_repo or ask.text(hint, current or detected or "")
    if not repo:
        console.print(
            "  [dim]Skipped — maajun will analyze errors and write reports "
            "to disk instead of opening PRs.[/dim]"
        )
        return
    if "/" not in repo and owner:
        repo = qualify(repo, owner)
        console.print(f"  [dim]Using {repo}[/dim]")
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

    # Updated in place: setup never asks about log_files, so rebuilding the
    # entry from these answers would drop them.
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

    setup_github_token(auth, config, ask, repo, reconfigure=reconfigure)


def setup_github_token(
    auth: AuthManager, config: Config, ask: Asker, repo: str, *, reconfigure: bool
) -> None:
    """Settle how maajun reaches GitHub, asking only when it has to."""
    if not reconfigure:
        source = auth.github_token_source()
        if source == "keyring":
            console.print("  [green]✓[/green] GitHub token already stored")
            choose_transport(config)
            report_push_access(auth, repo)
            return
        if source == "gh":
            account = gh_account()
            console.print(
                "  [green]✓[/green] Using your GitHub CLI login"
                + (f" as [cyan]{account}[/cyan]" if account else "")
                + " [dim](nothing to store)[/dim]"
            )
            choose_transport(config)
            report_push_access(auth, repo)
            return

    if not ask.interactive:
        console.print(
            "  [yellow]⚠ No GitHub credential. Run 'maajun login' to pick "
            "one — issues and PRs fail until you do.[/yellow]"
        )
        return

    if not authenticate(auth, config, repo):
        console.print(
            "  [yellow]⚠ No credential set — run 'maajun login' to try "
            "again.[/yellow]"
        )
        return
    report_push_access(auth, repo)


def choose_transport(config: Config) -> None:
    """Push over SSH when the machine's keys already work.

    Keeps the token to the API, where it is unavoidable, instead of putting
    it in front of every git push.
    """
    if config.github.transport != "auto" or not ssh_works():
        return
    config.github.transport = "ssh"
    console.print("  [green]✓[/green] Pushing over SSH [dim](your keys work)[/dim]")


def setup_error_sources(
    auth: AuthManager,
    config: Config,
    ask: Asker,
    *,
    log_paths: str | None,
) -> None:
    # Always: without knowing where this app's errors land, the daemon has
    # nothing to watch, and a finished setup would be a lie.
    for entry in config.github.repos:
        record_deployment(entry, config, auth)

    # Only asked when it is still needed: a repo that already knows where its
    # errors land does not need a path typed in as well.
    covered = any(sources for _, sources in config.sources_by_repo())
    if log_paths is not None or not covered:
        logs = log_paths if log_paths is not None else ask.text(
            "Log files to watch (comma-separated)",
            ",".join(config.monitor.log_files),
        )
        config.monitor.log_files = [
            path.strip() for path in logs.split(",") if path.strip()
        ]
    for path in config.monitor.log_files:
        if Path(path).expanduser().exists():
            console.print(f"  [green]✓[/green] {path}")
        else:
            # Fine: the app may only create its error log on first use.
            console.print(f"  [yellow]⚠[/yellow] {path} [dim](not found yet)[/dim]")

    # Runtime errors — the failed requests users actually hit — reach maajun
    # through one of the three sinks or not at all.
    unwatched = [
        repo_config.repo for repo_config, sources in config.sources_by_repo()
        if repo_config and not sources and repo_config.deployment.runtime != "none"
    ]
    if unwatched:
        console.print(
            "  [yellow]⚠ Nothing watches runtime errors for "
            f"{', '.join(unwatched)}. Run 'maajun discover --save' once the "
            "app is deployed on this host.[/yellow]"
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
    ask = Asker(interactive=not non_interactive)
    auth = AuthManager()
    config = load_config(path)

    console.print(Panel(
        "[bold]Maajun setup[/bold]\n\n"
        "Only the API key is required — press Enter to skip anything else.",
        border_style="blue",
    ))

    total = 3
    step(1, total, "AI provider")
    config.ai.provider = setup_provider(
        auth, ask, provider, reconfigure, config.ai.base_url
    )

    step(2, total, "GitHub", optional=True)
    setup_github(
        auth, config, ask,
        requested_repo=repo, base_branch=base_branch, mode=mode,
        test_command=test_command, reconfigure=reconfigure,
    )

    step(3, total, "Error sources")
    setup_error_sources(auth, config, ask, log_paths=logs)

    config.save(path)
    console.print(f"\n[green]✓ Wrote {path}[/green]")
    if not print_summary(config, auth):
        return
    if ask.interactive and ask.confirm("\nStart watching now?", default=True):
        start_watching(config, path)


def start_watching(config: Config, config_path: Path) -> None:
    """Launch the daemon, so setup ends with maajun actually running."""
    workdir = config.daemon.workdir
    if service.running(workdir):
        console.print("[dim]Already watching.[/dim]")
        return
    started = service.start(workdir, ["--config", str(config_path)])
    console.print(
        f"[green]✓ Watching in the background[/green] [dim](pid {started.pid})[/dim]\n"
        f"  [dim]Logs: tail -f {started.log_file} · stop: maajun watch --stop[/dim]"
    )


def print_summary(config: Config, auth: AuthManager) -> bool:
    repos = config.github.repos
    has_token = auth.has_github_token()
    sections, ok = build_status(
        config,
        provider=config.ai.provider,
        has_key=auth.has_api_key(config.ai.provider),
        has_token=has_token,
        repos=repos,
        network=None,
        probe=probe_source,
        token_source=auth.github_token_source(),
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
        return False
    console.print("\n[bold]You're ready.[/bold]")
    if repos:
        console.print("  [cyan]maajun watch[/cyan]            "
                      "[dim]watch in the background[/dim]")
        console.print("  [cyan]maajun watch --dry-run[/cyan]  "
                      "[dim]analyze without opening PRs[/dim]")
    else:
        console.print("  [cyan]maajun watch[/cyan]  "
                      "[dim]analyze errors into local reports[/dim]")
        console.print("  [dim]Run 'maajun setup' again to connect GitHub "
                      "and open PRs instead.[/dim]")
    return True
