from maajun.cli import (  # noqa: F401,E402  register commands
    chat,
    credentials,
    deployment,
    incidents,
    monitor,
    settings,
    setup,
)
from maajun.cli.shared import app, console

__all__ = ["app", "console"]
