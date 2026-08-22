from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from maajun.auth import AuthManager
from maajun.cli.deployment import record_deployment
from maajun.cli.shared import (
    app,
    console,
    load_config,
    prompt_line,
    prompt_secret,
)
from maajun.cli.status_checks import describe_transport, gather_github
from maajun.config import Config, RepoConfig
from maajun.vcs import GitHubClient, GitHubError
from maajun.vcs.gh import (
    INSTALL_GH,
    SSH_SETUP_URL,
    TOKEN_URL,
    gh_account,
    gh_available,
    gh_login,
    gh_token,
    ssh_works,
)

METHOD_GH = "gh"
METHOD_TOKEN = "token"
METHOD_SSH = "ssh"


def report_push_access(auth: AuthManager, repo: str) -> None:
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


def describe_current(auth: AuthManager, config: Config) -> None:
    """Say what maajun would use right now, before anything is changed."""
    source = auth.github_token_source()
    if source == "gh":
        account = gh_account()
        console.print(
            "  [green]✓[/green] GitHub CLI login"
            + (f" as [cyan]{account}[/cyan]" if account else "")
        )
    elif source == "keyring":
        console.print("  [green]✓[/green] Token stored in the keyring")
    else:
        console.print("  [yellow]⚠[/yellow] No GitHub credential yet")
    if source:
        console.print(f"  [dim]Pushing over {describe_transport(config, True)}[/dim]")


def choices(auth: AuthManager) -> list[tuple[str, str, str]]:
    """(method, label, detail) for each way of authenticating, best first."""
    options = []
    if gh_available():
        options.append((
            METHOD_GH,
            "GitHub CLI",
            "opens a browser login; maajun then stores nothing",
        ))
    else:
        options.append((
            METHOD_GH,
            "GitHub CLI [dim](not installed)[/dim]",
            "shows how to install it, then logs in",
        ))
    options.append((
        METHOD_TOKEN,
        "Personal access token",
        "paste a fine-grained token; stored in the OS keyring",
    ))
    options.append((
        METHOD_SSH,
        "SSH keys for pushing",
        "use your keys for branches; still needs one of the above for the API",
    ))
    return options


def pick_method(auth: AuthManager, default: str = METHOD_GH) -> str:
    """Ask how to authenticate. Returns the chosen method."""
    options = choices(auth)
    console.print("\n[bold]How should maajun reach GitHub?[/bold]")
    for index, (method, label, detail) in enumerate(options, 1):
        marker = " [dim](recommended)[/dim]" if method == default else ""
        console.print(f"  [cyan]{index}.[/cyan] {label}{marker}")
        console.print(f"     [dim]{detail}[/dim]")
    default_index = next(
        (i for i, (method, _, _) in enumerate(options, 1) if method == default), 1
    )
    while True:
        answer = prompt_line(f"\n> Choice [{default_index}]: ").strip()
        if not answer:
            return options[default_index - 1][0]
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        console.print("[red]Pick one of the numbers above.[/red]")


def run_gh_login() -> bool:
    """Hand the terminal to `gh auth login`. True if a token results."""
    if not gh_available():
        console.print(f"[dim]{INSTALL_GH}[/dim]")
        return False
    console.print("[dim]Handing over to the GitHub CLI…[/dim]\n")
    try:
        # Inherits this terminal: gh runs its own browser/device-code flow.
        completed = gh_login()
    except OSError as e:
        console.print(f"[red]✗ Could not run gh: {e}[/red]")
        return False
    if completed != 0:
        console.print("[yellow]⚠ gh login did not complete.[/yellow]")
        return False
    if not gh_token():
        console.print("[yellow]⚠ gh finished but reports no token.[/yellow]")
        return False
    account = gh_account()
    console.print(
        "[green]✓ Logged in with the GitHub CLI[/green]"
        + (f" as [cyan]{account}[/cyan]" if account else "")
    )
    return True


def store_token(auth: AuthManager, repo: str = "") -> bool:
    """Ask for a fine-grained token and keep it in the keyring."""
    scope = f" scoped to {repo}" if repo else ""
    console.print(
        f"  [dim]Create one at {TOKEN_URL}{scope}, with\n"
        "    Contents: read/write and Pull requests: read/write.[/dim]"
    )
    token = prompt_secret("> Token (input hidden): ").strip()
    if not token:
        console.print("[yellow]⚠ Nothing entered.[/yellow]")
        return False
    try:
        auth.set_github_token(token)
    except RuntimeError as e:
        console.print(f"[red]✗ Could not store it: {e}[/red]")
        return False
    console.print("[green]✓ Token stored.[/green]")
    return True


def use_ssh(config: Config) -> bool:
    """Record SSH as the push transport, if the keys actually work."""
    if not ssh_works():
        console.print(
            "[yellow]⚠ GitHub did not accept an SSH key from this machine.[/yellow]\n"
            f"[dim]Add one: {SSH_SETUP_URL}[/dim]"
        )
        return False
    config.github.transport = "ssh"
    console.print("[green]✓ Branches will be pushed over SSH.[/green]")
    return True


def authenticate(auth: AuthManager, config: Config, repo: str = "") -> bool:
    """Run the chosen method. True if maajun can reach the API afterwards."""
    method = pick_method(auth, default=default_method(auth))
    if method == METHOD_GH:
        run_gh_login()
    elif method == METHOD_TOKEN:
        store_token(auth, repo)
    else:
        use_ssh(config)
        if not auth.has_github_token():
            console.print(
                "  [dim]SSH pushes branches, but issues and pull requests go "
                "through the API. Pick a credential for that too.[/dim]"
            )
            if pick_method(auth, default=METHOD_GH) == METHOD_GH:
                run_gh_login()
            else:
                store_token(auth, repo)

    if auth.has_github_token() and config.github.transport == "auto" and ssh_works():
        config.github.transport = "ssh"
        console.print("[dim]Your SSH keys work, so pushes will use them.[/dim]")
    return auth.has_github_token()


def default_method(auth: AuthManager) -> str:
    """What to offer first: gh when it can be used, a token otherwise."""
    return METHOD_GH if gh_available() else METHOD_TOKEN


@app.command()
def login(
    config_path: Path | None = typer.Option(
        None, "--config", "-c", help="Config file location"
    ),
):
    """Choose how maajun authenticates with GitHub."""
    path = config_path
    config = load_config(path)
    auth = AuthManager()

    console.print("[bold]GitHub access[/bold]")
    describe_current(auth, config)

    if not authenticate(auth, config):
        console.print(
            "\n[yellow]No credential set. Issues and pull requests will "
            "fail until there is one.[/yellow]"
        )
        raise typer.Exit(1)

    config.save(path)
    repos = config.github.get_all_repos()
    if not repos:
        console.print(
            "\n[dim]No repository configured yet. "
            "Add one with 'maajun add-repo <owner/name>'.[/dim]"
        )
        return

    for repo_config in repos:
        report_push_access(auth, repo_config.repo)

    # Access alone is not enough to catch anything: a repo maajun can push to
    # but cannot read errors from is still a repo nobody is watching.
    console.print("\n[bold]Where each repo's errors land[/bold]")
    for repo_config in repos:
        record_deployment(repo_config, config, auth)
    config.save(path)
    console.print("\n[dim]Check it with 'maajun status'.[/dim]")
