"""Tests for CLI awareness, execution, and gating."""

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


def _tool(tools, name):
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

    return _tool(command_tools(), "run_maajun_command")


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


async def test_listing_commands_marks_the_ones_it_cannot_run():
    from maajun.chat.tools.commands import command_tools

    listing = await _tool(command_tools(), "list_maajun_commands")()
    assert "status" in listing
    assert "[cannot be run here]" in listing
    watch_line = next(
        line for line in listing.splitlines() if line.startswith("watch")
    )
    assert "[cannot be run here]" in watch_line


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


def _recording_confirm(answer):
    asked = []

    def confirm(prompt):
        asked.append(prompt)
        return answer

    return confirm, asked


async def test_read_only_commands_run_without_asking():
    confirm, asked = _recording_confirm(False)
    approve = chat_permissions(confirm)

    assert await approve("run_maajun_command", {"command": "status"}) is True
    assert asked == []


async def test_reading_a_config_value_does_not_ask():
    confirm, asked = _recording_confirm(False)
    approve = chat_permissions(confirm)

    assert await approve(
        "run_maajun_command", {"command": "config", "args": "github.mode"}
    ) is True
    assert asked == []


async def test_setting_a_config_value_asks_first():
    confirm, asked = _recording_confirm(True)
    approve = chat_permissions(confirm)

    assert await approve(
        "run_maajun_command", {"command": "config", "args": "github.mode fix"}
    ) is True
    assert asked == ["maajun config github.mode fix"]


async def test_a_declined_mutation_is_not_run():
    confirm, _ = _recording_confirm(False)
    approve = chat_permissions(confirm)

    assert await approve(
        "run_maajun_command", {"command": "add-repo", "args": "acme/api"}
    ) is False


async def test_blocked_commands_are_denied_without_asking():
    confirm, asked = _recording_confirm(True)
    approve = chat_permissions(confirm)

    assert await approve("run_maajun_command", {"command": "reset"}) is False
    assert asked == []


async def test_file_edits_still_ask():
    confirm, asked = _recording_confirm(True)
    approve = chat_permissions(confirm)

    assert await approve("edit_file", {"path": "/tmp/x.py"}) is True
    assert asked == ["edit_file /tmp/x.py"]


def test_the_confirmation_shows_the_exact_command():
    assert describe(
        "run_maajun_command", {"command": "add-repo", "args": "acme/api -m fix"}
    ) == "maajun add-repo acme/api -m fix"


def test_the_confirmation_omits_empty_arguments():
    assert describe("run_maajun_command", {"command": "status"}) == "maajun status"
