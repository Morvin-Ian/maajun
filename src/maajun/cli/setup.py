
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
    split_list,
)
from maajun.cli.status_checks import build_status
from maajun.config import Config, RepoConfig, default_config_path
from maajun.daemon import service
from maajun.project.discovery import probe_source
from maajun.project.toolchain import Check, detect_checks
from maajun.providers.base import ModelInfo, ProviderType
from maajun.providers.catalog import CatalogEntry, by_vendor, fetch_catalog
from maajun.providers.factory import ProviderFactory
from maajun.providers.pricing import base_pricing
from maajun.utils import is_valid_repo, qualify
from maajun.vcs.gh_cli import account_login, gh_account, ssh_works

PROVIDER_SIGNUP_URLS = {
    "deepseek": "https://platform.deepseek.com",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openrouter": "https://openrouter.ai/settings/keys",
    "straitly": "https://straitly.ai/",
    "bai": "https://chat.b.ai",
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


def split_by_kind(names: list[str]) -> tuple[list[str], list[str]]:
    """The providers as vendors and gateways.

    A gateway is the one with no catalogue of its own: it fronts other
    vendors' models rather than serving any it makes.
    """
    vendors = [name for name in names if provider_class(name).models]
    gateways = [name for name in names if not provider_class(name).models]
    return vendors, gateways


def pick_provider(ask: Asker, implemented: list[str], current: str) -> str:
    """Offer the providers down the page and grouped, not as one slashed line.

    Returns whatever was typed; the caller checks it against `implemented`.
    """
    vendors, gateways = split_by_kind(implemented)
    ordered = vendors + gateways
    if ask.interactive:
        number = 1
        for group, heading, note in (
            (vendors, "Vendors", "their own models"),
            (gateways, "Gateways", "one key, many vendors' models"),
        ):
            if not group:
                continue
            console.print(f"\n  [bold]{heading}[/bold] [dim]({note})[/dim]")
            for name in group:
                console.print(f"    [cyan]{number}.[/cyan] {name}")
                number += 1

    answer = ask.text("AI provider (number, or a name)", current).strip()
    if answer.isdigit() and 1 <= int(answer) <= len(ordered):
        return ordered[int(answer) - 1]
    return answer


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
        provider = pick_provider(ask, implemented, provider)
        if provider not in implemented:
            console.print(f"[red]✗ Unknown provider {provider!r}.[/red]")
            raise typer.Exit(1)

    if auth.has_api_key(provider) and not reconfigure:
        console.print(f"  [green]✓[/green] {provider} API key already stored")
        return provider

    say_where_credentials_go()

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


def provider_class(provider: str):
    return ProviderFactory.providers[ProviderType(provider)]


def model_line(cls, model: ModelInfo) -> str:
    """One catalogue entry: what it costs, and the role it already plays."""
    rates, known = base_pricing(model.id)
    price = (
        f"${rates['input']:.2f} in / ${rates['output']:.2f} out per 1M tokens"
        if known
        else "price unknown — costed at the dearest rate"
    )
    roles = []
    if model.id == cls.default_model:
        roles.append("default")
    if model.id == cls.thinking_model:
        roles.append("thinking_mode picks this")
    tag = f" [yellow]({', '.join(roles)})[/yellow]" if roles else ""
    return f"[cyan]{model.id}[/cyan] — [dim]{price}[/dim]{tag}"


def warn_if_unpriced(model: str, quoted: CatalogEntry | None = None) -> None:
    """Say so when a model has no pricing entry, because the cap will bite.

    An unknown model is costed at the dearest rate maajun knows, so the
    daily cap stops early rather than overshooting — right, but surprising
    if nobody said it would happen, and more so having just been shown what
    the gateway charges. Where there is a quote, it is repeated here so the
    gap between the two numbers is on screen rather than a mystery.
    """
    if not model or base_pricing(model)[1] is not None:
        return
    quote = ""
    if quoted is not None and quoted.input is not None and quoted.output is not None:
        quote = (
            f" [dim]The gateway quotes ${quoted.input:.2f} in / "
            f"${quoted.output:.2f} out.[/dim]"
        )
    console.print(
        f"  [yellow]⚠ No published price for {model}.[/yellow] [dim]It will be "
        "costed at the dearest rate maajun knows, so daemon.max_usd_per_day "
        f"will stop earlier than the real spend warrants.[/dim]{quote}"
    )


def pick_from_catalog(ask: Asker, cls, current: str | None) -> str | None:
    """Offer the provider's models. Returns the id, or None for its default."""
    console.print("\n  [bold]Models:[/bold]")
    for index, model in enumerate(cls.models, 1):
        console.print(f"    [cyan]{index}.[/cyan] {model_line(cls, model)}")
        console.print(f"       [dim]{model.note}[/dim]")

    default = current or cls.default_model
    answer = ask.text(
        "Model (number, or an id to use one not listed)", default
    ).strip()
    if answer.isdigit() and 1 <= int(answer) <= len(cls.models):
        answer = cls.models[int(answer) - 1].id
    # Storing the provider's own default pins it; leaving it unset lets the
    # default move when the provider's cheap tier is replaced.
    return None if answer == cls.default_model else answer


def catalog_line(entry: CatalogEntry) -> str:
    """One fetched model, at the price the gateway itself quotes for it."""
    if entry.input is None or entry.output is None:
        price = "price not quoted"
    elif not entry.input and not entry.output:
        price = "free"
    else:
        price = f"${entry.input:.2f} in / ${entry.output:.2f} out per 1M tokens"
    return f"[cyan]{entry.id}[/cyan] — [dim]{price}[/dim]"


def pick_from_gateway(
    ask: Asker, cls, entries: tuple[CatalogEntry, ...], current: str | None
) -> str | None:
    """Vendor first, then that vendor's models.

    Two steps because one is not enough: a gateway fronts hundreds of
    models, which is the shape their own catalogues take too. Either prompt
    also takes an id outright, for anyone who already knows the one they
    want.
    """
    groups = by_vendor(entries)
    vendors = list(groups)
    console.print(
        f"\n  [bold]{cls.name} carries {len(entries)} models from "
        f"{len(vendors)} vendors:[/bold]"
    )
    for index, vendor in enumerate(vendors, 1):
        console.print(
            f"    [cyan]{index}.[/cyan] {vendor} [dim]({len(groups[vendor])})[/dim]"
        )

    answer = ask.text("Vendor (number, or a model id to skip ahead)", "").strip()
    if not (answer.isdigit() and 1 <= int(answer) <= len(vendors)):
        return answer or None

    chosen = groups[vendors[int(answer) - 1]]
    console.print("\n  [bold]Models:[/bold]")
    for index, entry in enumerate(chosen, 1):
        console.print(f"    [cyan]{index}.[/cyan] {catalog_line(entry)}")

    answer = ask.text("Model (number, or an id)", current or "").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(chosen):
        return chosen[int(answer) - 1].id
    return answer or None


def ask_for_gateway_model(ask: Asker, cls, current: str | None) -> str | None:
    """A gateway has no default, so a model id is not optional."""
    console.print(
        f"\n  [dim]{cls.name} reaches many vendors' models, named like "
        f"{cls.model_example}. Browse them at {cls.catalog_url}[/dim]"
    )
    answer = ask.text(f"Model (e.g. {cls.model_example})", current or "").strip()
    if not answer:
        console.print(
            "  [yellow]⚠ No model set.[/yellow] [dim]A gateway has no default, "
            "so set one with 'maajun config ai.model <id>' before watching."
            "[/dim]"
        )
        return None
    return answer


def gateway_catalog(cls, api_key: str | None) -> tuple[CatalogEntry, ...]:
    """What the gateway says it carries, or () if it will not say."""
    if not api_key:
        return ()
    with console.status(f"[dim]Reading {cls.name}'s model list...[/dim]"):
        return fetch_catalog(cls.base_url, api_key)


def setup_model(
    ask: Asker,
    config: Config,
    provider: str,
    requested: str | None,
    api_key: str | None = None,
) -> None:
    """Choose which of the provider's models runs the investigations."""
    cls = provider_class(provider)
    if requested:
        config.ai.model = requested
        console.print(f"  [green]✓[/green] Model: {requested}")
        warn_if_unpriced(requested)
        return
    if not ask.interactive:
        return

    entries: tuple[CatalogEntry, ...] = ()
    if cls.models:
        chosen = pick_from_catalog(ask, cls, config.ai.model)
    else:
        # A gateway ships no catalogue, so its own /v1/models is the only place
        # the real ids and prices exist. Unreachable, and it asks for one instead.
        entries = gateway_catalog(cls, api_key)
        chosen = (
            pick_from_gateway(ask, cls, entries, config.ai.model)
            if entries
            else ask_for_gateway_model(ask, cls, config.ai.model)
        )
    config.ai.model = chosen
    settled = chosen or cls.default_model
    if not settled:
        return  # a gateway the user skipped; it already said so
    console.print(f"  [green]✓[/green] Model: {settled}")
    warn_if_unpriced(
        settled, next((e for e in entries if e.id == settled), None)
    )


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


def checkout_candidates(repo: str, entry: RepoConfig | None) -> list[Path]:
    """Local directories that might hold a checkout of `repo`."""
    candidates = []
    cwd = Path.cwd()
    if detect_repo_from_git(cwd) == repo:
        candidates.append(cwd)
    deployed = entry.deployment.path if entry else ""
    if deployed and Path(deployed) not in candidates:
        candidates.append(Path(deployed))
    return candidates


def detected_checks(repo: str, entry: RepoConfig | None) -> list[Check]:
    """Lint and format checks read from a local checkout, or none found."""
    for root in checkout_candidates(repo, entry):
        checks = detect_checks(root)
        if checks:
            return checks
    return []


def ask_verification_commands(
    ask: Asker, repo: str, entry: RepoConfig | None, current: list[str]
) -> list[str]:
    """Prompt for post-fix checks, prefilled with what the checkout implies.

    A suggestion only: detecting nothing just leaves an empty prompt.
    """
    detected = detected_checks(repo, entry) if not current else []
    for check in detected:
        console.print(f"  [dim]Detected from {check.source}: {check.command}[/dim]")
    default = ", ".join(current or [check.command for check in detected])
    answer = ask.text(
        "Verification commands, comma-separated (Enter to skip)", default
    )
    return split_list(answer) or []


def setup_github(
    auth: AuthManager,
    config: Config,
    ask: Asker,
    *,
    requested_repo: str | None,
    base_branch: str | None,
    mode: str | None,
    test_command: str | None,
    verification_commands: list[str] | None,
    reproduction_command: str | None,
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

    entry = next((rc for rc in existing if rc.repo == repo), None)

    branch = base_branch or ask.text(
        "Base branch", entry.base_branch if entry else "main"
    )
    resolved_mode = mode or (
        prompt_mode(entry.mode if entry else "suggest")
        if ask.interactive else (entry.mode if entry else "suggest")
    )

    # Fix and evidence-ready automatic mode can produce a diff to verify.
    current_test_command = entry.test_command if entry else ""
    current_verification_commands = entry.verification_commands if entry else []
    current_reproduction_command = entry.reproduction_command if entry else ""
    resolved_test_command = (
        test_command if test_command is not None else current_test_command
    )
    resolved_verification_commands = (
        verification_commands
        if verification_commands is not None
        else current_verification_commands
    )
    resolved_reproduction_command = (
        reproduction_command
        if reproduction_command is not None
        else current_reproduction_command
    )
    if resolved_mode in ("fix", "automatic"):
        if test_command is None and ask.interactive:
            console.print(
                "  [dim]Fix-capable modes use owner-controlled commands to "
                "verify a proposed change.[/dim]"
            )
            resolved_test_command = ask.text(
                "Test command (Enter to skip)", current_test_command
            )
        if verification_commands is None and ask.interactive:
            resolved_verification_commands = ask_verification_commands(
                ask, repo, entry, current_verification_commands
            )
        if (
            resolved_mode == "automatic"
            and reproduction_command is None
            and ask.interactive
        ):
            resolved_reproduction_command = ask.text(
                "Reproduction command (must fail before and pass after)",
                current_reproduction_command,
            )
    if resolved_mode == "fix":
        if not (
            resolved_test_command
            or resolved_verification_commands
            or resolved_reproduction_command
        ):
            console.print(
                "  [yellow]⚠ No test command or other verification commands — "
                "fix-mode PRs will be marked "
                "unverified.[/yellow]"
            )
    elif resolved_mode == "automatic" and not (
        resolved_reproduction_command
        and (resolved_test_command or resolved_verification_commands)
    ):
        console.print(
            "  [yellow]⚠ Automatic mode will remain read-only until both a "
            "reproduction command and a post-fix verification command are "
            "configured.[/yellow]"
        )

    # Updated in place: setup never asks about log_files, so rebuilding the
    # entry from these answers would drop them.
    if entry is not None:
        entry.base_branch = branch
        entry.mode = resolved_mode
        entry.test_command = resolved_test_command
        entry.verification_commands = resolved_verification_commands
        entry.reproduction_command = resolved_reproduction_command
    else:
        config.add_repo(RepoConfig(
            repo=repo, base_branch=branch, mode=resolved_mode,
            test_command=resolved_test_command,
            verification_commands=resolved_verification_commands,
            reproduction_command=resolved_reproduction_command,
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
    model: str | None = typer.Option(
        None, "--model", help="Model to use, e.g. gpt-4o (default: the provider's)"
    ),
    repo: str | None = typer.Option(
        None, "--repo", help="Repository to open PRs on (owner/name)"
    ),
    base_branch: str | None = typer.Option(
        None, "--base-branch", "-b", help="Branch to open PRs against"
    ),
    mode: str | None = typer.Option(
        None, "--mode", "-m", help="'suggest', 'fix', or 'automatic'"
    ),
    test_command: str | None = typer.Option(
        None, "--test-command",
        help="Command that verifies a fix-mode edit, e.g. 'pytest -q'",
    ),
    verification_commands: list[str] | None = typer.Option(
        None, "--verify-command",
        help="Post-fix command to run independently; repeat for more commands",
    ),
    reproduction_command: str | None = typer.Option(
        None, "--reproduction-command",
        help="Command expected to fail before a fix and pass after it",
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
    setup_model(
        ask, config, config.ai.provider, model,
        api_key=auth.get_api_key(config.ai.provider),
    )

    step(2, total, "GitHub", optional=True)
    setup_github(
        auth, config, ask,
        requested_repo=repo, base_branch=base_branch, mode=mode,
        test_command=test_command,
        verification_commands=verification_commands,
        reproduction_command=reproduction_command,
        reconfigure=reconfigure,
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
