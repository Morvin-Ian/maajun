from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from maajun.cli.shared import app, console, load_config
from maajun.config import Config
from maajun.daemon.core import LOCAL_REPO_LABEL
from maajun.daemon.store import MAX_ATTEMPTS, IncidentStore, StoreError
from maajun.utils import truncate, utc_day_start_iso

STATUS_STYLES = {"processed": "green", "failed": "red", "new": "yellow"}


def format_links(url: str | None) -> str:
    if not url:
        return "—"
    if url.startswith("http"):
        number = url.rstrip("/").rsplit("/", 1)[-1]
        label = f"#{number}" if number.isdigit() else number
        return f"[link={url}]{label}[/link]"
    # Local mode records a report path rather than a URL.
    return truncate(url.rsplit("/", 1)[-1], 24, "…")


@app.command()
def incidents(
    config_path: Path | None = typer.Option(
        None, "--config", "-c", help="Config file location"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="How many to show"),
    failed: bool = typer.Option(
        False, "--failed", help="Only incidents that failed and are no longer retried"
    ),
    repo: str | None = typer.Option(
        None, "--repo", "-r", help="Only incidents attributed to this repo (owner/name)"
    ),
    forget: str | None = typer.Option(
        None, "--forget",
        help="Forget this fingerprint, so the error is reported again if it returns",
    ),
):
    """List handled incidents with their repo, status, cost, and links."""
    config = load_config(config_path)
    workdir = Path(config.daemon.workdir).expanduser()
    database = workdir / "incidents.db"
    if not database.exists():
        console.print(
            "[dim]No incidents yet. "
            "Run [bold]maajun watch[/bold] first.[/dim]"
        )
        return

    try:
        store = IncidentStore(database)
    except StoreError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e
    try:
        if forget:
            forget_incident(store, forget, repo)
            return
        if repo is not None and repo not in store.repos():
            console.print(f"[yellow]No incidents recorded for {repo}.[/yellow]")
            known = [name for name in store.repos() if name]
            if known:
                console.print(f"[dim]Repos with incidents: {', '.join(known)}[/dim]")
            return
        rows = store.exhausted() if failed else store.all(repo)
        if failed and repo is not None:
            rows = [row for row in rows if row["repo"] == repo]
        # After the --repo filter, and for --failed too, which ignored it.
        rows = rows[:limit]
        render_incidents(store, rows, config, failed=failed)
    finally:
        store.close()


def caught_by(source: str) -> str:
    """Which kind of source found an incident: "docker", "manual", …

    The target is in the artifact; here the kind is what tells a reported
    issue apart from one a monitor caught.
    """
    if not source:
        return "—"
    kind = source.split(":", 1)[0]
    return "report" if kind == "manual" else kind


def forget_incident(store: IncidentStore, fingerprint: str, repo: str | None) -> None:
    """Drop one incident's record, so a recurrence is treated as new.

    For when the fix is in and you want to hear about it immediately if it
    comes back, rather than after daemon.reopen_after_days.
    """
    matches = [
        row for row in store.all()
        if row["fingerprint"].startswith(fingerprint)
        and (repo is None or row["repo"] == repo)
    ]
    if not matches:
        console.print(f"[yellow]No incident starting with {fingerprint}.[/yellow]")
        return
    if len(matches) > 1 and repo is None:
        console.print(
            f"[yellow]{len(matches)} incidents start with {fingerprint} — "
            "add --repo, or give more of the fingerprint.[/yellow]"
        )
        for row in matches:
            console.print(f"  {row['fingerprint']}  {row['repo'] or LOCAL_REPO_LABEL}")
        return
    row = matches[0]
    store.forget_artifact(row["fingerprint"], row["repo"])
    console.print(
        f"[green]✓ Forgot {row['fingerprint']}[/green] "
        f"[dim]({truncate(row['message'], 50, '…')})[/dim]\n"
        "[dim]It will be reported again the next time it happens.[/dim]"
    )


def render_incidents(
    store: IncidentStore, rows: list[dict], config: Config, *, failed: bool
) -> None:
    title = "Exhausted incidents" if failed else "Incidents"
    if not rows:
        message = (
            f"[green]No incidents have failed {MAX_ATTEMPTS} times.[/green]"
            if failed
            else "[dim]No incidents recorded yet.[/dim]"
        )
        console.print(message)
        if not failed:
            return

    if rows:
        show_repo = len({row["repo"] for row in rows}) > 1
        # Shown only when it separates rows — a reported issue among ones a
        # monitor caught. Otherwise it is a column of the same word.
        show_source = len({caught_by(row["source"]) for row in rows}) > 1
        table = Table(title=title)
        table.add_column("Fingerprint", style="dim", no_wrap=True)
        if show_repo:
            table.add_column("Repo", no_wrap=True)
        table.add_column("Status")
        if show_source:
            table.add_column("Caught by", no_wrap=True)
        table.add_column("Error")
        table.add_column("Seen", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Issue / PR", no_wrap=True)

        for row in rows:
            status = row["status"]
            style = STATUS_STYLES.get(status, "dim")
            label = status
            if status == "failed":
                label = f"{status} ({row['attempts']}/{MAX_ATTEMPTS})"
            cells = [row["fingerprint"]]
            if show_repo:
                cells.append(row["repo"] or LOCAL_REPO_LABEL)
            cells.append(f"[{style}]{label}[/{style}]")
            if show_source:
                cells.append(caught_by(row["source"]))
            cells.extend([
                truncate(row["message"], 60, "…"),
                str(row["count"]),
                f"${row['cost_usd']:.4f}" if row["cost_usd"] else "—",
                format_links(row["pr_url"]),
            ])
            table.add_row(*cells)
        console.print(table)

    render_totals(store, config)


def render_totals(store: IncidentStore, config: Config) -> None:
    tokens = store.total_tokens()
    spent_today = store.cost_since(utc_day_start_iso())
    cap = config.daemon.max_usd_per_day

    lines = [
        f"Today:    [bold]${spent_today:.4f}[/bold]"
        + (f" of ${cap:g} cap" if cap > 0 else " [dim](no cap set)[/dim]"),
        f"All time: [bold]${store.total_cost():.4f}[/bold]"
        f"  [dim]({tokens['prompt_tokens']:,} prompt + "
        f"{tokens['completion_tokens']:,} completion tokens)[/dim]",
    ]
    if cap > 0 and spent_today >= cap:
        lines.append(
            "\n[yellow]⚠ Cap reached — analysis is paused until the next "
            "UTC day.[/yellow]"
        )
    elif cap <= 0:
        lines.append(
            "\n[dim]Set a ceiling with "
            "'maajun config daemon.max_usd_per_day 5'.[/dim]"
        )

    exhausted = store.exhausted()
    if exhausted:
        lines.append(
            f"\n[red]{len(exhausted)} incident(s) failed {MAX_ATTEMPTS} times "
            "and are no longer retried — see 'maajun incidents --failed'.[/red]"
        )

    console.print(Panel("\n".join(lines), title="Spend", border_style="blue"))
