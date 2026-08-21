"""Tests for the chat REPL: turns, slash commands, and persistence."""

import pytest
from rich.console import Console

from maajun.chat.memory import ChatMemory
from maajun.chat.prompt import build_system_prompt
from maajun.chat.session import COMMANDS, HELP, ChatSession
from maajun.config import AIProviderConfig, Config, GitHubConfig, RepoConfig
from maajun.daemon.store import IncidentStore
from maajun.providers.base import CompletionResponse, ProviderError


class ScriptedAgent:
    """Stands in for the real Agent: records prompts, replays canned replies.

    Streams like the real one does — a reply arrives in pieces, and the usage
    it cost is read afterwards rather than returned.
    """

    def __init__(self, replies=(), error=None, model="deepseek-v4-flash"):
        self.replies = list(replies)
        self.error = error
        self.model = model
        self.prompts = []
        self.history = []
        self.closed = False
        self.usage = {}

    async def chat_stream(self, message):
        self.prompts.append(message)
        reply = (
            self.replies.pop(0)
            if self.replies
            else CompletionResponse(
                content="ok",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        )
        self.usage = dict(reply.usage or {})
        if self.error:
            raise self.error
        for chunk in (reply.content or "").split(" "):
            yield "content", chunk + " "

    def take_usage(self):
        usage, self.usage = self.usage, {}
        return usage

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
    opened = []

    def build(lines, *, agent=None, config=None):
        database = tmp_path / "incidents.db"
        store = IncidentStore(database)
        memory = ChatMemory(database)
        # Tracked so the fixture can close it: an unclosed handle per session
        # is a ResourceWarning on every run, which buries real ones.
        out = open(tmp_path / "out.txt", "a")
        opened.append(out)
        console = Console(file=out, width=100)
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
    for handle in opened:
        handle.close()


def printed(session):
    """Everything printed so far, whitespace collapsed.

    Rich wraps at the console width, so an asserted phrase can break across
    two lines depending on how long an interpolated path or title is. Flatten
    first and the assertion stops depending on where the wrap lands — the
    same reason tests/cli/test_commands.py has flat().
    """
    session.console.file.flush()
    with open(session.console.file.name) as handle:
        return " ".join(handle.read().split())


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


def test_a_turn_reaches_the_agent_and_prints_the_answer(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="Two repos are configured.")])
    session = session_factory(["how many repos?"], agent=agent)
    session.loop()

    assert agent.prompts == ["how many repos?"]
    assert "Two repos are configured." in printed(session)


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

    assert "Rate limit reached." in printed(session)


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
    assert "kaboom" in printed(session)


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
    assert "Slash commands" in printed(session)


def test_slash_commands_lists_the_cli(session_factory):
    session = session_factory(["/commands"])
    session.loop()
    output = printed(session)
    assert "add-repo" in output
    assert "not from chat" in output  # watch/reset are flagged


def test_slash_clear_resets_context_but_keeps_the_record(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="noted")])
    session = session_factory(["remember this", "/clear"], agent=agent)
    session.loop()

    assert agent.history == []
    assert len(session.memory.messages(session.session_id)) == 2
    assert "still on record" in printed(session)


def test_slash_history_replays_the_session(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="the answer")])
    session = session_factory(["the question", "/history"], agent=agent)
    session.loop()

    output = printed(session)
    assert "the question" in output
    assert "the answer" in output


def test_slash_cost_reports_the_session_spend(session_factory):
    agent = ScriptedAgent([CompletionResponse(
        content="hi", usage={"prompt_tokens": 1000, "completion_tokens": 500},
    )])
    session = session_factory(["hello", "/cost"], agent=agent)
    session.loop()

    output = printed(session)
    assert "This session" in output
    assert "chat cap: $5.00" in output


def test_slash_sessions_marks_the_current_one(session_factory):
    session = session_factory(["/sessions"])
    session.loop()
    assert "this one" in printed(session)


def test_an_unknown_slash_command_suggests_help(session_factory):
    agent = ScriptedAgent()
    session = session_factory(["/nonsense"], agent=agent)
    session.loop()

    assert agent.prompts == []
    assert "Unknown command" in printed(session)


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
    assert "maajun add-repo acme/api" in printed(session)


def test_declining_says_so(session_factory):
    session = session_factory(["n"])
    session.confirm("maajun reset")
    assert "Skipped" in printed(session)


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
    assert "acme/api" in printed(session)


def test_the_greeting_says_so_in_local_mode(session_factory):
    session = session_factory([])
    session.greet()
    assert "local mode" in printed(session)


# ---------------------------------------------------------------------------
# The event loop the turns run on
# ---------------------------------------------------------------------------


class LoopWatcher(ScriptedAgent):
    """Records the event loop each turn runs on."""

    def __init__(self):
        super().__init__()
        self.loops = []

    async def chat_stream(self, message):
        import asyncio

        self.loops.append(asyncio.get_running_loop())
        async for chunk in super().chat_stream(message):
            yield chunk


def test_every_turn_runs_on_the_same_event_loop(session_factory):
    """A fresh loop per turn strands the provider's pooled connection on the
    dead one, and the first request of every later turn fails and retries."""
    agent = LoopWatcher()
    session = session_factory(["one", "two", "three"], agent=agent)
    session.loop()

    assert len(agent.loops) == 3
    assert len(set(id(loop) for loop in agent.loops)) == 1


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


def test_a_turn_that_fails_still_records_what_it_spent(session_factory):
    """The rounds before the failure were billed whether or not they answered."""
    agent = ScriptedAgent([CompletionResponse(
        content="never arrives",
        usage={"prompt_tokens": 900, "completion_tokens": 100},
    )], error=ProviderError("boom"))
    session = session_factory(["hello"], agent=agent)
    session.loop()

    row = session.memory.session(session.session_id)
    assert row["prompt_tokens"] == 900
    assert row["cost_usd"] > 0


def test_the_daily_cap_stops_a_turn_before_it_is_sent(session_factory):
    from maajun.config import AIProviderConfig, ChatConfig, Config

    config = Config(
        ai=AIProviderConfig(provider="deepseek", api_key="x"),
        chat=ChatConfig(max_usd_per_day=0.01),
    )
    agent = ScriptedAgent()
    session = session_factory(["hello"], agent=agent, config=config)
    session.memory.record_usage(session.session_id, cost_usd=0.5)
    session.loop()

    assert agent.prompts == []
    assert "cap" in printed(session)


def test_no_cap_means_no_ceiling(session_factory):
    from maajun.config import AIProviderConfig, ChatConfig, Config

    config = Config(
        ai=AIProviderConfig(provider="deepseek", api_key="x"),
        chat=ChatConfig(max_usd_per_day=0),
    )
    agent = ScriptedAgent()
    session = session_factory(["hello"], agent=agent, config=config)
    session.memory.record_usage(session.session_id, cost_usd=500)
    session.loop()

    assert agent.prompts == ["hello"]


# ---------------------------------------------------------------------------
# Slash commands vs. messages that merely start with a slash
# ---------------------------------------------------------------------------


def test_a_path_at_the_start_of_a_message_is_not_a_command(session_factory):
    agent = ScriptedAgent()
    session = session_factory(["/var/log/app.log is full of errors"], agent=agent)
    session.loop()

    assert agent.prompts == ["/var/log/app.log is full of errors"]
    assert "Unknown command" not in printed(session)


def test_slash_new_starts_a_separate_session(session_factory):
    session = session_factory(["/new"])
    first = session.session_id
    session.loop()

    assert session.session_id != first
    assert session.memory.session(session.session_id) is not None


def test_slash_resume_carries_an_earlier_session_on(session_factory):
    session = session_factory(["/resume"])
    earlier = session.memory.start_session()
    session.memory.add_message(earlier, "user", "we discussed fix mode")

    session.resume(str(earlier))

    assert session.session_id == earlier
    assert [m["content"] for m in session.agent.history] == ["we discussed fix mode"]


def test_slash_resume_needs_a_real_session(session_factory):
    session = session_factory([])
    session.resume("999")
    assert "No chat session 999" in printed(session)


def test_slash_forget_deletes_a_conversation(session_factory):
    session = session_factory([])
    earlier = session.memory.start_session()
    session.memory.add_message(earlier, "user", "something private")

    session.forget(str(earlier))

    assert session.memory.session(earlier) is None
    assert session.memory.search("private") == []


def test_slash_forget_refuses_the_live_conversation(session_factory):
    session = session_factory([])
    session.forget(str(session.session_id))

    assert session.memory.session(session.session_id) is not None
    assert "this conversation" in printed(session)


def test_slash_forget_all_asks_first(session_factory):
    session = session_factory(["n"])
    other = session.memory.start_session()

    session.forget("all")

    assert session.memory.session(other) is not None


def test_slash_model_reports_the_current_model(session_factory):
    session = session_factory([])
    session.switch_model("")
    assert "deepseek-v4-flash" in printed(session)


def test_slash_provider_rejects_an_unknown_name(session_factory):
    session = session_factory([])
    session.switch_provider("gemini")
    assert "Unknown provider" in printed(session)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_the_answer_is_printed_as_it_streams(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="one two three")])
    session = session_factory(["go"], agent=agent)
    session.loop()

    assert "one two three" in printed(session)


def test_a_streamed_answer_is_recorded_whole(session_factory):
    agent = ScriptedAgent([CompletionResponse(content="one two three")])
    session = session_factory(["go"], agent=agent)
    session.loop()

    recorded = session.memory.messages(session.session_id)[-1]
    assert recorded["content"] == "one two three"


def test_a_confirmation_takes_the_spinner_off_the_screen(session_factory):
    """A Live redraw and a prompt cannot share the same lines."""
    from maajun.chat.session import TurnView

    session = session_factory(["y"])
    session.view = TurnView(session.console)
    session.view.waiting()

    assert session.confirm("maajun add-repo acme/api") is True
    assert session.view.live is None


# ---------------------------------------------------------------------------
# Approving, refusing, and redirecting a tool call
# ---------------------------------------------------------------------------


def test_a_yes_approves_one_call(session_factory):
    session = session_factory(["y"])
    assert session.ask_permission("maajun add-repo acme/api") is True


@pytest.mark.parametrize("answer", ["n", "no", ""])
def test_a_no_declines_without_a_reason(answer, session_factory):
    session = session_factory([answer])
    assert session.ask_permission("maajun add-repo acme/api") is False


def test_anything_else_is_passed_on_as_an_instruction(session_factory):
    session = session_factory(["use acme/web instead"])
    assert session.ask_permission("maajun add-repo acme/api") == "use acme/web instead"


def test_always_stops_asking_for_that_tool(session_factory):
    from maajun.chat.permissions import chat_permissions

    session = session_factory(["a"])
    approve = chat_permissions(session.ask_permission)

    async def run():
        first = await approve("edit_file", {"path": "/tmp/a.py"})
        second = await approve("edit_file", {"path": "/tmp/b.py"})
        return first, second

    import asyncio

    first, second = asyncio.run(run())
    assert first is True
    assert second is True  # the driver has no second answer to give


def test_a_reason_reaches_the_model(session_factory):
    from maajun.agent.core import PERMISSION_DENIED, Agent
    from maajun.chat.permissions import chat_permissions
    from maajun.config import AIProviderConfig, Config

    session = session_factory(["not that file, edit the other one"])
    agent = Agent(
        Config(ai=AIProviderConfig(provider="deepseek", api_key="x")),
        approve=chat_permissions(session.ask_permission),
    )

    import asyncio

    result = asyncio.run(agent.execute_tool("edit_file", {"path": "/tmp/a.py"}))
    assert result.startswith(PERMISSION_DENIED)
    assert "not that file" in result


def test_reasoning_is_not_printed(session_factory):
    """A model thinking out loud is talking to itself, not to the user."""

    class Thinker(ScriptedAgent):
        async def chat_stream(self, message):
            self.prompts.append(message)
            yield "thinking", "The user probably means the checkout bug. Let me..."
            yield "content", "It was a KeyError."

    session = session_factory(["what was it?"], agent=Thinker())
    session.loop()

    output = printed(session)
    assert "It was a KeyError." in output
    assert "Let me" not in output


def test_the_spinner_can_change_phase_while_it_is_running(session_factory):
    """Regression: the label was read back off the Live, which wraps it."""
    from maajun.chat.session import TurnView

    session = session_factory([])
    view = TurnView(session.console)
    view.waiting()
    view.waiting("Running run_maajun_command")

    assert view.status.phase == "Running run_maajun_command"
    view.close()


def test_a_tool_call_is_announced_before_it_runs(session_factory):
    class Worker(ScriptedAgent):
        async def chat_stream(self, message):
            self.prompts.append(message)
            yield "running", "run_maajun_command"
            yield "tool", "🔧 run_maajun_command → done"
            yield "content", "Ready."

    session = session_factory(["is it ready?"], agent=Worker())
    session.loop()

    output = printed(session)
    assert "run_maajun_command" in output
    assert "Ready." in output


# ---------------------------------------------------------------------------
# Text that is not markup
# ---------------------------------------------------------------------------


def test_an_error_containing_a_closing_tag_does_not_crash_the_turn(session_factory):
    """Rich parses square brackets. A provider error quoting the model back
    can carry "[/INST]" and friends, and an unmatched closing tag is a
    MarkupError — the reported failure taking down the loop reporting it."""
    session = session_factory([])

    def explode(coro):
        coro.close()  # the turn never awaits it; closing keeps the run quiet
        raise RuntimeError("model returned [/INST] unexpectedly")

    session.runner.run = explode
    session.turn("hello")

    assert "[/INST]" in printed(session)


def test_a_provider_error_with_markup_is_shown_literally(session_factory):
    session = session_factory([])

    def explode(coro):
        coro.close()
        raise ProviderError("rate limited on [model/v2]")

    session.runner.run = explode
    session.turn("hello")

    assert "[model/v2]" in printed(session)


def test_the_watch_notice_escapes_the_message():
    """A daemon notice carries an error message or a log line verbatim."""
    import io

    from maajun.cli import monitor as monitor_cli

    console = Console(file=io.StringIO(), width=200)
    original = monitor_cli.console
    monitor_cli.console = console

    class FakeDaemon:
        progress = None
        on_notice = None

        async def run(self, **kwargs):
            self.on_notice("failed on [/red] input", "error")

    try:
        monitor_cli.watch_with_spinner(FakeDaemon(), once=True)
    finally:
        monitor_cli.console = original

    assert "[/red]" in console.file.getvalue()


# ---------------------------------------------------------------------------
# Slash dispatch
# ---------------------------------------------------------------------------


def test_every_advertised_slash_command_has_a_handler(session_factory):
    session = session_factory([])
    handlers = session.slash_handlers()
    missing = [name for name in COMMANDS if name not in handlers]
    assert not missing, f"advertised in COMMANDS but not handled: {missing}"


def test_every_handler_is_advertised(session_factory):
    session = session_factory([])
    extra = [name for name in session.slash_handlers() if name not in COMMANDS]
    assert not extra, f"handled but not offered for completion: {extra}"


def test_every_slash_command_is_documented_in_help(session_factory):
    session = session_factory([])
    for name in session.slash_handlers():
        assert name in HELP, f"{name} is not in /help"


# ---------------------------------------------------------------------------
# Rebuilding the agent
# ---------------------------------------------------------------------------


def test_switching_model_carries_the_conversation_over(session_factory):
    """Same conversation, different model."""
    session = session_factory([])
    session.agent.history = [{"role": "user", "content": "remember this"}]

    session.replace_agent()

    assert session.agent.history == [{"role": "user", "content": "remember this"}]


def test_starting_a_new_session_does_not(session_factory):
    """/new, /resume and /forget used to carry the history over and then
    clear it, which read as though the old context might survive."""
    session = session_factory([])
    session.agent.history = [{"role": "user", "content": "old talk"}]

    session.replace_agent(keep_history=False)

    assert session.agent.history == []
