from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from maajun.cli._shared import app, console
from maajun.config import Config
from maajun.daemon.store import MAX_ATTEMPTS, IncidentStore
from maajun.utils import truncate, utc_day_start_iso

_STATUS_STYLES = {"processed": "green", "failed": "red", "new": "yellow"}


def _link(url: str | None) -> str:
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
):
    """List handled incidents with their status, cost, and links."""
    config = Config.load(config_path)
    workdir = Path(config.daemon.workdir).expanduser()
    database = workdir / "incidents.db"
    if not database.exists():
        console.print(
            f"[dim]No incidents yet — {database} does not exist. "
            "Run [bold]maajun watch[/bold] first.[/dim]"
        )
        return

    store = IncidentStore(database)
    try:
        rows = store.exhausted() if failed else store.all()[:limit]
        _render(store, rows, config, failed=failed)
    finally:
        store.close()


def _render(
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
        table = Table(title=title)
        table.add_column("Fingerprint", style="dim", no_wrap=True)
        table.add_column("Status")
        table.add_column("Error")
        table.add_column("Seen", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Issue / PR", no_wrap=True)

        for row in rows:
            status = row["status"]
            style = _STATUS_STYLES.get(status, "dim")
            label = status
            if status == "failed":
                label = f"{status} ({row['attempts']}/{MAX_ATTEMPTS})"
            table.add_row(
                row["fingerprint"],
                f"[{style}]{label}[/{style}]",
                truncate(row["message"], 60, "…"),
                str(row["count"]),
                f"${row['cost_usd']:.4f}" if row["cost_usd"] else "—",
                _link(row["pr_url"]),
            )
        console.print(table)

    _render_totals(store, config)


def _render_totals(store: IncidentStore, config: Config) -> None:
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
