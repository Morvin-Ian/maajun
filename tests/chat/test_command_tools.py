"""Tests for CLI awareness, execution, and gating."""

import io

import pytest

from maajun.chat.permissions import chat_permissions, describe
from maajun.chat.tools.commands import (
    BLOCKED,
    Gate,
    classify,
    command_help,
    command_index,
    parse_args,
    run_cli,
)


def tool(tools, name):
    return next(t for t in tools if t.definition.name == name).executor


# ---------------------------------------------------------------------------
# Discovering the command surface
# ---------------------------------------------------------------------------


def test_the_index_is_read_from_the_live_cli():
    """Not a hand-written list: a new command must show up on its own."""
    names = {info.name for info in command_index()}
    assert {"setup", "status", "watch", "report", "incidents", "config"} <= names


def test_every_command_has_a_summary():
    assert all(info.help for info in command_index())


def test_the_index_carries_the_gate():
    gates = {info.name: info.gate for info in command_index()}
    assert gates["status"] is Gate.READ_ONLY
    assert gates["add-repo"] is Gate.MUTATING
    assert gates["watch"] is Gate.BLOCKED


def test_command_help_returns_the_real_flag_list():
    text = command_help("add-repo")
    assert "--base-branch" in text
    assert "--mode" in text


def test_command_help_on_an_unknown_command_names_the_real_ones():
    text = command_help("nope")
    assert "No such command: nope" in text
    assert "status" in text


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_reading_a_config_value_is_read_only():
    assert classify("config", []) is Gate.READ_ONLY
    assert classify("config", ["github.mode"]) is Gate.READ_ONLY


def test_setting_a_config_value_is_mutating():
    assert classify("config", ["github.mode", "fix"]) is Gate.MUTATING


def test_flags_do_not_count_as_the_config_value():
    """'config github.mode -c f.toml' still only reads."""
    assert classify("config", ["github.mode", "-c", "f.toml"]) is Gate.READ_ONLY


def test_an_unknown_command_defaults_to_mutating():
    """The safe default when a command is added and nobody classified it."""
    assert classify("some-future-command") is Gate.MUTATING


@pytest.mark.parametrize("name", sorted(BLOCKED))
def test_blocked_commands_are_blocked(name):
    assert classify(name) is Gate.BLOCKED


# ---------------------------------------------------------------------------
# Running commands in-process
# ---------------------------------------------------------------------------


def test_run_cli_captures_output_and_exit_code(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[ai]\nprovider = \"deepseek\"\n")

    code, output = run_cli(["config", "-c", str(config)])
    assert code == 0
    assert "provider" in output


def test_run_cli_reports_a_failing_command_without_raising(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[ai]\nprovider = \"deepseek\"\n")

    code, output = run_cli(["config", "nonsense.key", "-c", str(config)])
    assert code != 0
    assert "nonsense.key" in output or "Unknown" in output


def test_run_cli_reports_a_bad_flag_as_output_not_an_exception():
    code, output = run_cli(["status", "--no-such-flag"])
    assert code != 0
    assert "no-such-flag" in output


def test_run_cli_restores_stdin_afterwards():
    import sys

    before = sys.stdin
    run_cli(["provider-list"])
    assert sys.stdin is before


def test_run_cli_leaves_stdout_alone_afterwards(capsys):
    run_cli(["provider-list"])
    print("still visible")
    assert "still visible" in capsys.readouterr().out


def test_parse_args_splits_like_a_shell():
    assert parse_args('github.test_command "pytest -q" -r acme/api') == [
        "github.test_command", "pytest -q", "-r", "acme/api",
    ]


def test_parse_args_on_empty_input():
    assert parse_args("") == []
    assert parse_args("   ") == []


# ---------------------------------------------------------------------------
# The run tool
# ---------------------------------------------------------------------------


@pytest.fixture
def run_tool():
    from maajun.chat.tools.commands import command_tools

    return tool(command_tools(), "run_maajun_command")


async def test_the_run_tool_reports_the_command_it_ran(run_tool, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[ai]\nprovider = \"deepseek\"\n")

    result = await run_tool(command="config", args=f"-c {config}")
    assert result.startswith("$ maajun config")
    assert "succeeded" in result


async def test_the_run_tool_refuses_watch(run_tool):
    result = await run_tool(command="watch")
    assert "Refusing to run 'watch'" in result
    assert "runs until interrupted" in result


async def test_the_run_tool_refuses_login(run_tool):
    """It prompts, and option 1 hands the terminal to gh — both would hang."""
    result = await run_tool(command="login")
    assert "Refusing to run 'login'" in result
    assert "your own terminal" in result


async def test_the_run_tool_refuses_reset(run_tool):
    result = await run_tool(command="reset", args="--force")
    assert "Refusing to run 'reset'" in result


async def test_the_run_tool_rejects_an_unknown_command(run_tool):
    result = await run_tool(command="deploy")
    assert "No such command: deploy" in result
    assert "status" in result


async def test_the_run_tool_reports_unbalanced_quotes(run_tool):
    result = await run_tool(command="config", args='github.mode "fix')
    assert "Could not parse the arguments" in result


async def test_bare_setup_is_refused_with_the_alternative(run_tool):
    result = await run_tool(command="setup")
    assert "--non-interactive" in result


async def test_a_failing_command_is_reported_not_raised(run_tool):
    result = await run_tool(command="config", args="nonsense.key")
    assert "failed (exit 1)" in result


# ---------------------------------------------------------------------------
# Quieting the terminal while the capture is held
# ---------------------------------------------------------------------------


async def test_the_run_tool_quiets_the_terminal_around_the_capture(tmp_path):
    """run_cli swaps sys.stdout process-wide from a worker thread. Rich
    resolves sys.stdout on every write, so a spinner left running on the main
    thread paints into the capture buffer instead of the terminal."""
    import contextlib

    from maajun.chat.tools.commands import command_tools

    events = []

    @contextlib.contextmanager
    def quiet():
        events.append("stopped")
        yield
        events.append("released")

    run = tool(command_tools(quiet), "run_maajun_command")
    config = tmp_path / "config.toml"
    config.write_text('[ai]\nprovider = "deepseek"\n')

    result = await run(command="config", args=f"-c {config}")
    assert "succeeded" in result
    assert events == ["stopped", "released"]


async def test_a_refused_command_does_not_quiet_the_terminal(tmp_path):
    """Nothing is captured, so there is no reason to take the spinner down."""
    import contextlib

    from maajun.chat.tools.commands import command_tools

    events = []

    @contextlib.contextmanager
    def quiet():
        events.append("stopped")
        yield

    run = tool(command_tools(quiet), "run_maajun_command")
    assert "Refusing to run 'watch'" in await run(command="watch")
    assert events == []


async def test_the_default_quiet_scope_is_a_no_op(tmp_path):
    """command_tools is usable without a session to quiet."""
    from maajun.chat.tools.commands import command_tools

    run = tool(command_tools(), "run_maajun_command")
    config = tmp_path / "config.toml"
    config.write_text('[ai]\nprovider = "deepseek"\n')
    assert "succeeded" in await run(command="config", args=f"-c {config}")


def test_the_chat_session_quiets_its_spinner(tmp_path):
    """The scope the session actually hands down stops the Live region."""
    from rich.console import Console

    from maajun.chat.session import ChatSession, TurnView

    console = Console(file=io.StringIO())
    session = ChatSession.__new__(ChatSession)
    session.view = TurnView(console)
    session.view.waiting("Running run_maajun_command")
    assert session.view.live is not None

    with ChatSession.quiet(session):
        assert session.view.live is None, "the spinner is down for the capture"


def test_the_session_scope_tolerates_no_active_turn():
    from maajun.chat.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session.view = None
    with ChatSession.quiet(session):
        pass


async def test_listing_commands_marks_the_ones_it_cannot_run():
    from maajun.chat.tools.commands import command_tools

    listing = await tool(command_tools(), "list_maajun_commands")()
    assert "status" in listing
    assert "[cannot be run here]" in listing
    watch_line = next(
        line for line in listing.splitlines() if line.startswith("watch")
    )
    assert "[cannot be run here]" in watch_line


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


def recording_confirm(answer):
    asked = []

    def confirm(prompt):
        asked.append(prompt)
        return answer

    return confirm, asked


async def test_read_only_commands_run_without_asking():
    confirm, asked = recording_confirm(False)
    approve = chat_permissions(confirm)

    assert await approve("run_maajun_command", {"command": "status"}) is True
    assert asked == []


async def test_reading_a_config_value_does_not_ask():
    confirm, asked = recording_confirm(False)
    approve = chat_permissions(confirm)

    assert await approve(
        "run_maajun_command", {"command": "config", "args": "github.mode"}
    ) is True
    assert asked == []


async def test_setting_a_config_value_asks_first():
    confirm, asked = recording_confirm(True)
    approve = chat_permissions(confirm)

    assert await approve(
        "run_maajun_command", {"command": "config", "args": "github.mode fix"}
    ) is True
    assert asked == ["maajun config github.mode fix"]


async def test_a_declined_mutation_is_not_run():
    confirm, _ = recording_confirm(False)
    approve = chat_permissions(confirm)

    assert await approve(
        "run_maajun_command", {"command": "add-repo", "args": "acme/api"}
    ) is False


async def test_blocked_commands_are_denied_without_asking():
    confirm, asked = recording_confirm(True)
    approve = chat_permissions(confirm)

    assert await approve("run_maajun_command", {"command": "reset"}) is False
    assert asked == []


async def test_file_edits_still_ask():
    confirm, asked = recording_confirm(True)
    approve = chat_permissions(confirm)

    assert await approve("edit_file", {"path": "/tmp/x.py"}) is True
    assert asked == ["edit_file /tmp/x.py"]


def test_the_confirmation_shows_the_exact_command():
    assert describe(
        "run_maajun_command", {"command": "add-repo", "args": "acme/api -m fix"}
    ) == "maajun add-repo acme/api -m fix"


def test_the_confirmation_omits_empty_arguments():
    assert describe("run_maajun_command", {"command": "status"}) == "maajun status"


# ---------------------------------------------------------------------------
# Running a command from inside the agent's event loop
# ---------------------------------------------------------------------------


async def test_a_command_that_runs_its_own_event_loop_still_works(monkeypatch):
    """`status` and `report` call asyncio.run inside; nesting one is fatal.

    The tool has to hand the CLI to a worker thread, or the two most useful
    commands in the index answer with a RuntimeError.
    """
    import asyncio

    from maajun.chat.tools import commands as module

    def cli_with_its_own_loop(argv):
        async def work():
            return "did the thing"

        return 0, asyncio.run(work())

    monkeypatch.setattr(module, "run_cli", cli_with_its_own_loop)
    run = tool(module.command_tools(), "run_maajun_command")

    result = await run("status")
    assert "did the thing" in result
    assert "RuntimeError" not in result


async def test_running_a_command_does_not_block_the_loop(monkeypatch):
    import asyncio
    import threading

    from maajun.chat.tools import commands as module

    started = threading.Event()

    def slow_cli(argv):
        started.wait(2)
        return 0, "finished"

    monkeypatch.setattr(module, "run_cli", slow_cli)
    run = tool(module.command_tools(), "run_maajun_command")

    task = asyncio.create_task(run("status"))
    await asyncio.sleep(0)
    started.set()
    assert "finished" in await task


# ---------------------------------------------------------------------------
# Output the model can read
# ---------------------------------------------------------------------------


def test_box_drawing_is_redrawn_in_ascii():
    from maajun.chat.tools.commands import plain

    table = "┏━━━━━┓\n┃ hi  ┃\n┗━━━━━┛"
    assert plain(table) == "| hi  |"


def test_plain_keeps_ordinary_output_intact():
    from maajun.chat.tools.commands import plain

    assert plain("  ✓ API key for deepseek\n") == "✓ API key for deepseek"


def test_output_is_captured_wider_than_a_default_terminal(monkeypatch):
    """80 columns breaks repo names and URLs across lines mid-word."""
    import os

    from maajun.chat.tools.commands import CAPTURE_WIDTH

    monkeypatch.delenv("COLUMNS", raising=False)
    seen = {}

    def record(argv):
        seen["columns"] = os.environ.get("COLUMNS")
        return 0, ""

    monkeypatch.setattr("maajun.chat.tools.commands.cli_command", lambda: Recorder(record))
    run_cli(["status"])
    assert seen["columns"] == CAPTURE_WIDTH
    assert "COLUMNS" not in os.environ


class Recorder:
    def __init__(self, record):
        self.record = record

    def main(self, args, **kwargs):
        self.record(args)
        return 0


# ---------------------------------------------------------------------------
# Seeing the change before approving it
# ---------------------------------------------------------------------------


def test_an_edit_is_described_by_its_diff():
    text = describe(
        "edit_file",
        {"path": "/tmp/app.py", "old_string": "x = 1", "new_string": "x = 2"},
    )
    assert "/tmp/app.py" in text
    assert "-x = 1" in text
    assert "+x = 2" in text


def test_a_new_file_is_described_by_its_size():
    text = describe("write_file", {"path": "/tmp/does-not-exist.py", "content": "hi"})
    assert "new file, 2 bytes" in text


def test_an_overwrite_is_described_by_its_diff(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old line\n")

    text = describe("write_file", {"path": str(target), "content": "new line\n"})
    assert "-old line" in text
    assert "+new line" in text


def test_a_long_diff_is_cut_short():
    from maajun.chat.permissions import DIFF_LINES

    text = describe(
        "edit_file",
        {
            "path": "/tmp/app.py",
            "old_string": "\n".join(f"old {n}" for n in range(200)),
            "new_string": "\n".join(f"new {n}" for n in range(200)),
        },
    )
    assert "more diff lines" in text
    assert len(text.splitlines()) < DIFF_LINES + 5


def test_the_description_names_the_absolute_path(tmp_path):
    """Whether the path is allowed at all is the sandbox's call, not this one."""
    text = describe("write_file", {"path": str(tmp_path / "app.py"), "content": "x"})
    assert str(tmp_path / "app.py") in text
