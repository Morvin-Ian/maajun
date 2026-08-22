from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shlex
import sys
import threading
from collections.abc import Callable, Iterator
from enum import Enum
from typing import NamedTuple

import typer
from typer.core import TyperGroup

from maajun.agent.tools.base import Tool, json_schema
from maajun.providers.base import ToolDefinition

try:
    # Typer >= 0.20 vendors click; the top-level one raises different classes.
    from typer._click import ClickException
except ImportError:  # pragma: no cover - typer built against the real click
    from click import ClickException

PROG_NAME = "maajun"

# Rich wraps to 80 with no terminal, breaking repo names and URLs.
CAPTURE_WIDTH = "120"

# The model reads the capture as text, so borders are redrawn in ASCII.
BOX = str.maketrans({
    "│": "|", "┃": "|", "║": "|",
    "─": "-", "━": "-", "═": "-",
    "┌": "+", "┐": "+", "└": "+", "┘": "+", "├": "+", "┤": "+",
    "┬": "+", "┴": "+", "┼": "+", "╭": "+", "╮": "+", "╰": "+", "╯": "+",
    "┏": "+", "┓": "+", "┗": "+", "┛": "+", "┡": "+", "┩": "+", "╇": "+",
    "┳": "+", "┻": "+", "╋": "+", "┠": "+", "┨": "+", "╞": "+", "╡": "+",
})


def plain(text: str) -> str:
    """Captured terminal output as prose: ASCII borders, no rule lines."""
    lines = []
    for line in text.translate(BOX).splitlines():
        stripped = line.strip()
        if stripped and not stripped.strip("+-| "):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


class Gate(Enum):
    """How much ceremony running a command needs."""

    READ_ONLY = "read-only"
    MUTATING = "mutating"
    BLOCKED = "blocked"


# Commands chat will not run: ones that never return, and ones too
# destructive to infer from a sentence.
BLOCKED: dict[str, str] = {
    "watch": (
        "it runs until interrupted, which would hang this session. "
        "Run it in your own terminal."
    ),
    "chat": "you are already in a chat session.",
    "login": (
        "it prompts, and can hand the terminal to the GitHub CLI. "
        "Run it in your own terminal."
    ),
    "reset": (
        "it permanently deletes all config, data, and credentials. "
        "Run it yourself if you mean it."
    ),
    "sign-out": (
        "it deletes stored credentials. Run it yourself if you mean it."
    ),
}

# Everything else that is not blocked is mutating, so a new command asks.
READ_ONLY: frozenset[str] = frozenset({"status", "incidents", "provider-list"})


class CommandInfo(NamedTuple):
    name: str
    help: str
    gate: Gate


def cli_command() -> TyperGroup:
    # Imported here: maajun.cli imports the chat command, which imports this.
    from maajun.cli import app

    return typer.main.get_command(app)


def context(command, info_name: str = PROG_NAME, parent=None):
    """A context built from the command's own class, not an imported one.

    command.context_class is whichever Context the installed typer vendors,
    which is the only one its commands will accept.
    """
    return command.context_class(command, info_name=info_name, parent=parent)


def parsed_params(name: str, argv: list[str]) -> dict | None:
    """What the CLI's own parser makes of `argv`, or None if it cannot.

    Used instead of eyeballing the arguments: "config github.mode -c f.toml"
    has two non-flag tokens but only one of them is the key, and only the
    parser knows which options take a value.
    """
    command = cli_command()
    try:
        with context(command) as ctx:
            sub = command.get_command(ctx, name)
            if sub is None:
                return None
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                sub_ctx = sub.make_context(
                    name, list(argv), parent=ctx, resilient_parsing=True
                )
            return sub_ctx.params
    except Exception:
        return None


def classify(name: str, argv: list[str] | None = None) -> Gate:
    """How `name` should be gated, given the arguments it would run with.

    `config` is the one command that is either: printing a value reads,
    passing a value writes.
    """
    if name in BLOCKED:
        return Gate.BLOCKED
    if name == "config":
        params = parsed_params("config", argv or [])
        if params is None:
            return Gate.MUTATING  # cannot tell — err towards asking
        return Gate.READ_ONLY if params.get("value") is None else Gate.MUTATING
    if name in READ_ONLY:
        return Gate.READ_ONLY
    return Gate.MUTATING


def command_index() -> list[CommandInfo]:
    """Every registered command, with its one-line help and gating."""
    command = cli_command()
    with context(command) as ctx:
        infos = []
        for name in sorted(command.list_commands(ctx)):
            sub = command.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            infos.append(
                CommandInfo(name, sub.get_short_help_str(limit=80) or "", classify(name))
            )
    return infos


def command_help(name: str) -> str:
    """The command's real --help text, or a message naming the valid ones.

    Captured from stdout, not taken from the return value: typer renders help
    through rich, which writes to the console and hands back an empty string.
    """
    command = cli_command()
    with context(command) as ctx:
        sub = command.get_command(ctx, name)
        if sub is None:
            known = ", ".join(info.name for info in command_index())
            return f"No such command: {name}. Available: {known}"
        buffer = io.StringIO()
        # The parent supplies "maajun"; passing it again renders it twice.
        with context(sub, info_name=name, parent=ctx) as sub_ctx:
            with contextlib.redirect_stdout(buffer):
                returned = sub.get_help(sub_ctx)
    return (returned or "").strip() or buffer.getvalue().strip()


@contextlib.contextmanager
def empty_stdin():
    """Swap stdin for an empty stream (contextlib has no redirect_stdin)."""
    original = sys.stdin
    sys.stdin = io.StringIO()
    try:
        yield
    finally:
        sys.stdin = original


@contextlib.contextmanager
def capture_width():
    original = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = CAPTURE_WIDTH
    try:
        yield
    finally:
        if original is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = original


# run_cli swaps stdout, stderr, stdin and $COLUMNS — process-wide state, from
# a worker thread. Held so two captures cannot interleave.
CAPTURE_LOCK = threading.Lock()


def run_cli(argv: list[str]) -> tuple[int, str]:
    """Run a maajun command in-process. Returns (exit code, combined output).

    In-process, not a subprocess: the CLI may not be on PATH, and this way it
    sees the config and keyring state the session already loaded. stdin is
    emptied so a command that would prompt fails instead of hanging.

    The capture is process-wide, so nothing else may write to the terminal
    while it runs — see the `quiet` scope in command_tools.
    """
    command = cli_command()
    buffer = io.StringIO()
    try:
        with (
            CAPTURE_LOCK,
            contextlib.redirect_stdout(buffer),
            contextlib.redirect_stderr(buffer),
            empty_stdin(),
            capture_width(),
        ):
            # standalone_mode=False makes click *return* a typer.Exit code,
            # so a clean failure looks like success unless it is read back.
            returned = command.main(
                args=argv, prog_name=PROG_NAME, standalone_mode=False
            )
    except ClickException as e:
        e.show(file=buffer)
        return e.exit_code, buffer.getvalue()
    except typer.Abort:
        return 1, buffer.getvalue() + "\nAborted."
    except SystemExit as e:
        return int(e.code or 0), buffer.getvalue()
    except EOFError:
        return 1, (
            buffer.getvalue()
            + "\nThe command asked for interactive input, which is not "
            "available here. Re-run it with the equivalent flags, or run it "
            "in your own terminal."
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the model, not swallowed
        return 1, f"{buffer.getvalue()}\n{type(e).__name__}: {e}"
    return (returned if isinstance(returned, int) else 0), buffer.getvalue()


def parse_args(args: str) -> list[str]:
    """Split an argument string the way a shell would, without running one."""
    return shlex.split(args) if args.strip() else []


@contextlib.contextmanager
def unquieted() -> Iterator[None]:
    """The default `quiet` scope: nothing else is drawing, so nothing to stop."""
    yield


QuietScope = Callable[[], contextlib.AbstractContextManager[None]]


def command_tools(quiet: QuietScope = unquieted) -> list[Tool]:
    """The three tools that let chat see and drive the CLI.

    `quiet` wraps the tool that captures stdout. Rich resolves sys.stdout on
    every write, so a spinner left running paints into the capture buffer
    instead of the terminal.
    """

    async def list_commands() -> str:
        lines = [
            f"{info.name:<16}{info.help}"
            + ("" if info.gate is not Gate.BLOCKED else "  [cannot be run here]")
            for info in command_index()
        ]
        return "\n".join(lines)

    async def get_command_help(command: str) -> str:
        return command_help(command)

    async def run_maajun_command(command: str, args: str = "") -> str:
        known = {info.name for info in command_index()}
        if command not in known:
            return (
                f"No such command: {command}. "
                f"Available: {', '.join(sorted(known))}"
            )
        try:
            argv = parse_args(args)
        except ValueError as e:
            return f"Could not parse the arguments: {e}"

        if classify(command, argv) is Gate.BLOCKED:
            return (
                f"Refusing to run '{command}': {BLOCKED[command]} "
                "Tell the user the command to run rather than running it."
            )
        if command == "setup" and "--non-interactive" not in argv:
            # setup exists to prompt, but can still run against a stored key.
            return (
                "'setup' prompts for input, which is not available here. Add "
                "--non-interactive with the flags you want (it cannot store a "
                "new API key that way), or tell the user to run 'maajun setup' "
                "in their terminal."
            )

        # In a thread: some commands call asyncio.run(), which cannot nest
        # inside the loop this tool call is already on.
        with quiet():
            exit_code, output = await asyncio.to_thread(run_cli, [command, *argv])
        rendered = plain(output) or "(no output)"
        status = "succeeded" if exit_code == 0 else f"failed (exit {exit_code})"
        return f"$ maajun {command} {args}".rstrip() + f"\n{status}\n\n{rendered}"

    return [
        Tool(
            ToolDefinition(
                name="list_maajun_commands",
                description=(
                    "List every maajun CLI command with a one-line summary. "
                    "Use when the user asks what maajun can do."
                ),
                parameters=json_schema({}),
            ),
            list_commands,
        ),
        Tool(
            ToolDefinition(
                name="maajun_command_help",
                description=(
                    "Full --help text for one maajun command, listing every "
                    "flag. Read this before running a command with arguments "
                    "you are not certain of."
                ),
                parameters=json_schema(
                    {
                        "command": {
                            "type": "string",
                            "description": "Command name, e.g. 'add-repo'",
                        },
                    },
                    required=["command"],
                ),
            ),
            get_command_help,
        ),
        Tool(
            ToolDefinition(
                name="run_maajun_command",
                description=(
                    "Run a maajun CLI command and return its output. "
                    "Read-only commands run immediately; anything that changes "
                    "configuration or opens a PR asks the user first. "
                    "'watch', 'login', 'reset', and 'sign-out' cannot be run "
                    "here — tell the user the command instead."
                ),
                parameters=json_schema(
                    {
                        "command": {
                            "type": "string",
                            "description": "Command name, e.g. 'status'",
                        },
                        "args": {
                            "type": "string",
                            "description": (
                                "Arguments as they would be typed, e.g. "
                                "\"github.mode fix -r acme/api\". Quote values "
                                "containing spaces."
                            ),
                        },
                    },
                    required=["command"],
                ),
            ),
            run_maajun_command,
            requires_permission=True,
        ),
    ]
