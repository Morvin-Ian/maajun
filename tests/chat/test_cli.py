"""Tests for the `maajun chat` command itself."""

import pytest
from typer.testing import CliRunner

from maajun.cli import app

runner = CliRunner()


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[ai]\nprovider = "deepseek"\n'
        f'[daemon]\nworkdir = "{tmp_path / "data"}"\n'
    )
    return path


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr("maajun.auth.get_keyring", lambda name: None)


@pytest.fixture
def deepseek_key(monkeypatch):
    monkeypatch.setattr(
        "maajun.auth.get_keyring",
        lambda name: "sk-test" if name == "deepseek_api_key" else None,
    )


def test_chat_is_registered():
    result = runner.invoke(app, ["--help"])
    assert "chat" in result.output


def test_without_a_key_it_says_to_run_setup(config, no_keys):
    result = runner.invoke(app, ["chat", "-c", str(config)])
    assert result.exit_code == 1
    assert "No API key for deepseek" in flat(result.output)
    assert "maajun setup" in flat(result.output)


def test_without_that_provider_it_names_the_ones_that_are_configured(
    config, deepseek_key
):
    result = runner.invoke(app, ["chat", "-c", str(config), "--provider", "openai"])
    assert result.exit_code == 1
    assert "No API key for openai" in flat(result.output)
    assert "Configured: deepseek" in flat(result.output)


def test_an_unknown_session_is_reported_not_a_traceback(config, deepseek_key):
    result = runner.invoke(app, ["chat", "-c", str(config), "--session", "999"])
    assert result.exit_code == 1
    assert "No chat session 999" in flat(result.output)
    assert "Traceback" not in result.output


def test_it_greets_and_exits_cleanly_on_end_of_input(config, deepseek_key):
    """No stdin is the CI case; it must not hang or crash."""
    result = runner.invoke(app, ["chat", "-c", str(config)], input="")
    assert result.exit_code == 0
    assert "Maajun chat" in flat(result.output)


def test_exit_leaves_without_calling_the_provider(config, deepseek_key):
    result = runner.invoke(app, ["chat", "-c", str(config)], input="/exit\n")
    assert result.exit_code == 0
    assert "Bye" in flat(result.output)


def test_help_works_without_reaching_the_provider(config, deepseek_key):
    result = runner.invoke(app, ["chat", "-c", str(config)], input="/help\n/exit\n")
    assert result.exit_code == 0
    assert "Slash commands" in flat(result.output)


def test_the_session_is_recorded_even_with_no_turns(config, deepseek_key, tmp_path):
    from maajun.chat.memory import ChatMemory

    runner.invoke(app, ["chat", "-c", str(config)], input="/exit\n")

    memory = ChatMemory(tmp_path / "data" / "incidents.db")
    assert len(memory.recent_sessions()) == 1
    memory.close()


def test_a_bad_provider_name_is_rejected_with_a_readable_message(
    config, deepseek_key
):
    """The field validator raises a ValidationError; it must not reach stdout.

    CliRunner stores an uncaught exception rather than printing it, so assert
    on the message being present, not on 'Traceback' being absent.
    """
    result = runner.invoke(app, ["chat", "-c", str(config), "--provider", "gemini"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Unknown provider 'gemini'" in flat(result.output)
    assert "deepseek, openai" in flat(result.output)
