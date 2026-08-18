from maajun.cli import (  # noqa: F401,E402  register commands
    chat,
    credentials,
    incidents,
    monitor,
    settings,
    setup,
)
from maajun.cli._shared import app, console

__all__ = ["app", "console"]
