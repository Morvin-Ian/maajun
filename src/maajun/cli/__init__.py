"""Maajun CLI. Commands live in domain modules but register on one shared
Typer `app`, so the CLI stays flat (`maajun login`, `maajun watch`, …).

`maajun.cli:app` is the console-script entry point; importing this package
pulls in every command module for its `@app.command()` side effects.
"""

from maajun.cli import (  # noqa: F401,E402  register commands
    auth,
    incidents,
    monitor,
    setup,
    wizard,
)
from maajun.cli._shared import app, console

__all__ = ["app", "console"]
