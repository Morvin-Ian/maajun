"""Tests for the chat REPL: turns, slash commands, and persistence."""

import pytest
from rich.console import Console

from maajun.chat.memory import ChatMemory
from maajun.chat.prompt import build_system_prompt
from maajun.chat.session import ChatSession
from maajun.config import AIProviderConfig, Config, GitHubConfig, RepoConfig
from maajun.daemon.store import IncidentStore
from maajun.providers.base import CompletionResponse, ProviderError


class ScriptedAgent:
    """Stands in for the real Agent: records prompts, replays canned replies."""

    def __init__(self, replies=(), error=None):
        self.replies = list(replies)
        self.error = error
        self.prompts = []
        self.history = []
        self.closed = False

    async def chat(self, message):
        self.prompts.append(message)
        if self.error:
            raise self.error
        if self.replies:
            return self.replies.pop(0)
        return CompletionResponse(content="ok", usage={
            "prompt_tokens": 10, "completion_tokens": 5,
        })

    def clear_history(self):
        self.history.clear()

    async def aclose(self):
        self.closed = True


class Driver:
    """Feeds scripted input to the session and captures what it prints."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


@pytest.fixture
def session_factory(tmp_path):
    made = []

    def build(lines, *, agent=None, config=None):
        database = tmp_path / "incidents.db"
        store = IncidentStore(database)
        memory = ChatMemory(database)
        console = Console(file=open(tmp_path / "out.txt", "a"), width=100)
        session = ChatSession(
            config or Config(ai=AIProviderConfig(provider="deepseek", api_key="x")),
            console=console,
            store=store,
            memory=memory,
            session_id=memory.start_session(),
            ask=Driver(lines),
        )
        session.agent = agent or ScriptedAgent()
        made.append(session)
        return session

    yield build
    for session in made:
        session.close()


def _output(session):
    """Everything printed so far, whitespace collapsed.

    Rich wraps at the console width, so an asserted phrase can break across
    two lines depending on how long an interpolated path or title is. Flatten
    first and the assertion stops depending on where the wrap lands — the
    same reason tests/cli/test_commands.py has flat().
    """
    session.console.file.flush()
    return " ".join(open(session.console.file.name).read().split())


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


def test_a_turn_reaches_the_agent_and_prints_the_answer(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="Two repos are configured.")])
    session = session_factory(["how many repos?"], agent=agent)
    session.loop()

    assert agent.prompts == ["how many repos?"]
    assert "Two repos are configured." in _output(session)


def test_both_sides_of_a_turn_are_recorded(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="the answer")])
    session = session_factory(["the question"], agent=agent)
    session.loop()

    recorded = [
        (m["role"], m["content"])
        for m in session.memory.messages(session.session_id)
    ]
    assert recorded == [("user", "the question"), ("assistant", "the answer")]


def test_usage_is_recorded_against_the_session(session_factory):
    agent = ScriptedAgent([CompletionResponse(
        content="hi", usage={"prompt_tokens": 300, "completion_tokens": 100},
        model="deepseek-v4-flash",
    )])
    session = session_factory(["hello"], agent=agent)
    session.loop()

    row = session.memory.session(session.session_id)
    assert row["prompt_tokens"] == 300
    assert row["completion_tokens"] == 100
    assert row["cost_usd"] > 0


def test_a_provider_error_is_shown_not_raised(session_factory):
    agent = ScriptedAgent(error=ProviderError("Rate limit reached."))
    session = session_factory(["hello"], agent=agent)
    session.loop()

    assert "Rate limit reached." in _output(session)


def test_a_failed_turn_records_no_answer(session_factory):
    agent = ScriptedAgent(error=ProviderError("boom"))
    session = session_factory(["hello"], agent=agent)
    session.loop()

    roles = [m["role"] for m in session.memory.messages(session.session_id)]
    assert roles == ["user"]


def test_an_unexpected_error_does_not_end_the_session(session_factory):
    agent = ScriptedAgent(error=RuntimeError("kaboom"))
    session = session_factory(["one", "two"], agent=agent)
    session.loop()

    assert agent.prompts == ["one", "two"]
    assert "kaboom" in _output(session)


def test_blank_input_is_skipped(session_factory):
    agent = ScriptedAgent()
    session = session_factory(["", "   ", "real question"], agent=agent)
    session.loop()

    assert agent.prompts == ["real question"]


@pytest.mark.parametrize("word", ["/exit", "/quit", "exit", "quit"])
def test_exit_words_end_the_session(word, session_factory):
    agent = ScriptedAgent()
    session = session_factory([word, "never asked"], agent=agent)
    session.loop()

    assert agent.prompts == []


def test_end_of_input_ends_the_session(session_factory):
    session = session_factory([])
    session.loop()  # the driver raises EOFError; must not propagate


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def test_slash_commands_do_not_reach_the_model(session_factory):
    agent = ScriptedAgent()
    session = session_factory(["/help"], agent=agent)
    session.loop()

    assert agent.prompts == []
    assert "Slash commands" in _output(session)


def test_slash_commands_lists_the_cli(session_factory):
    session = session_factory(["/commands"])
    session.loop()
    output = _output(session)
    assert "add-repo" in output
    assert "not from chat" in output  # watch/reset are flagged


def test_slash_clear_resets_context_but_keeps_the_record(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="noted")])
    session = session_factory(["remember this", "/clear"], agent=agent)
    session.loop()

    assert agent.history == []
    assert len(session.memory.messages(session.session_id)) == 2
    assert "still on record" in _output(session)


def test_slash_history_replays_the_session(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="the answer")])
    session = session_factory(["the question", "/history"], agent=agent)
    session.loop()

    output = _output(session)
    assert "the question" in output
    assert "the answer" in output


def test_slash_cost_reports_the_session_spend(session_factory):
    agent = ScriptedAgent([CompletionResponse(
        content="hi", usage={"prompt_tokens": 1000, "completion_tokens": 500},
    )])
    session = session_factory(["hello", "/cost"], agent=agent)
    session.loop()

    output = _output(session)
    assert "This session" in output
    assert "not capped" in output


def test_slash_sessions_marks_the_current_one(session_factory):
    session = session_factory(["/sessions"])
    session.loop()
    assert "this one" in _output(session)


def test_an_unknown_slash_command_suggests_help(session_factory):
    agent = ScriptedAgent()
    session = session_factory(["/nonsense"], agent=agent)
    session.loop()

    assert agent.prompts == []
    assert "Unknown command" in _output(session)


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("Y", True), ("yes", True), ("YES", True),
    ("n", False), ("", False), ("no", False), ("maybe", False),
])
def test_confirmation_only_accepts_yes(answer, expected, session_factory):
    session = session_factory([answer])
    assert session.confirm("maajun add-repo acme/api") is expected


def test_the_confirmation_shows_the_command(session_factory):
    session = session_factory(["n"])
    session.confirm("maajun add-repo acme/api")
    assert "maajun add-repo acme/api" in _output(session)


def test_declining_says_so(session_factory):
    session = session_factory(["n"])
    session.confirm("maajun reset")
    assert "Skipped" in _output(session)


# ---------------------------------------------------------------------------
# Resuming
# ---------------------------------------------------------------------------


def test_resume_replays_an_earlier_session_into_context(session_factory):
    agent = ScriptedAgent()
    session = session_factory([], agent=agent)
    earlier = session.memory.start_session()
    session.memory.add_message(earlier, "user", "we discussed fix mode")
    session.memory.add_message(earlier, "assistant", "yes, on acme/api")

    session.resume_from(earlier)
    assert [m["content"] for m in agent.history] == [
        "we discussed fix mode", "yes, on acme/api",
    ]


def test_resume_is_capped_at_the_recent_tail(session_factory):
    from maajun.chat.session import RESUME_MESSAGES

    agent = ScriptedAgent()
    session = session_factory([], agent=agent)
    earlier = session.memory.start_session()
    for n in range(RESUME_MESSAGES * 2):
        session.memory.add_message(earlier, "user", f"message {n}")

    session.resume_from(earlier)
    assert len(agent.history) == RESUME_MESSAGES


# ---------------------------------------------------------------------------
# The system prompt
# ---------------------------------------------------------------------------


def test_the_system_prompt_lists_the_real_commands():
    prompt = build_system_prompt()
    assert "add-repo" in prompt
    assert "incidents" in prompt


def test_the_system_prompt_flags_what_chat_cannot_run():
    prompt = build_system_prompt()
    watch_line = next(
        line for line in prompt.splitlines() if line.startswith("- watch:")
    )
    assert "cannot be run from chat" in watch_line


def test_the_greeting_names_the_configured_repos(session_factory):
    config = Config(
        ai=AIProviderConfig(provider="deepseek", api_key="x"),
        github=GitHubConfig(repos=[RepoConfig(repo="acme/api")]),
    )
    session = session_factory([], config=config)
    session.greet()
    assert "acme/api" in _output(session)


def test_the_greeting_says_so_in_local_mode(session_factory):
    session = session_factory([])
    session.greet()
    assert "local mode" in _output(session)
