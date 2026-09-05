from maajun.cli import (  # noqa: F401,E402  register commands
    chat,
    credentials,
    deployment,
    github_auth,
    incidents,
    promotion,
    settings,
    setup,
    watch,
)
from maajun.cli.shared import app, console

__all__ = ["app", "console"]
