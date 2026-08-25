import asyncio
import json

import pytest

from maajun.agent.core import (
    MAX_CONTINUATIONS,
    PERMISSION_DENIED,
    Agent,
    Correction,
)
from maajun.agent.tools import WRITE_FILE, ToolRegistry, default_registry
from maajun.agent.tools.base import Tool, json_schema
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import CompletionResponse, ProviderError, ToolDefinition
from maajun.providers.factory import ProviderFactory

# ---------------------------------------------------------------------------
# Fake provider that simulates tool-calling rounds
# ---------------------------------------------------------------------------


class StreamsFromChatMixin:
    """stream_completion that mirrors chat_completion as stream events."""

    async def stream_completion(self, messages, **kwargs):
        response = await self.chat_completion(messages, **kwargs)
        if response.thinking:
            yield "thinking", response.thinking
        if response.content:
            yield "content", response.content
        if response.tool_calls:
            yield "tool_calls", response.tool_calls


@pytest.fixture(autouse=True)
def isolate_cwd(monkeypatch, tmp_path):
    """Any tool that writes a relative path must land in tmp, never the repo.

    write_file is a real executor here, so without this a fake tool call with
    a relative path drops files into the working tree.
    """
    monkeypatch.chdir(tmp_path)


class ToolCallingProvider(StreamsFromChatMixin):
    """Provider that returns tool_calls on the first call, then plain text."""

    def __init__(self, tool_result="tool output", reply="final answer", path="x.txt"):
        self.call_count = 0
        self.tool_result = tool_result
        self.reply = reply
        self.path = path
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.call_count += 1
        self.last_messages = messages
        if self.call_count == 1:
            # First call: return a tool_call
            return CompletionResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": self.path, "content": "hi"}),
                        },
                    }
                ],
            )
        # Second call: return final answer
        return CompletionResponse(content=self.reply)

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


class MultiToolProvider(StreamsFromChatMixin):
    """Provider that makes 3 tool calls across 2 rounds."""

    def __init__(self):
        self.call_count = 0
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.call_count += 1
        self.last_messages = messages
        if self.call_count == 1:
            return CompletionResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "a.txt", "content": "a"}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "b.txt", "content": "b"}),
                        },
                    },
                ],
            )
        if self.call_count == 2:
            return CompletionResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "c.txt", "content": "c"}),
                        },
                    }
                ],
            )
        return CompletionResponse(content="done")

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


class FailToolProvider(StreamsFromChatMixin):
    """Provider that tries a tool that fails, then gives up."""

    def __init__(self):
        self.call_count = 0
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.call_count += 1
        self.last_messages = messages
        if self.call_count == 1:
            return CompletionResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {"path": "/nonexistent/file.txt"}
                            ),
                        },
                    }
                ],
            )
        return CompletionResponse(content="recovered")

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    return Config(ai=AIProviderConfig(provider="deepseek", api_key="x"))


def make_agent(config, provider):
    """Create an agent with a custom provider and empty registry."""
    agent = Agent(config)
    agent.provider = provider
    agent.registry = ToolRegistry()
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_agent_executes_tool_and_returns_final_answer(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)

    response = await agent.chat("do something")

    assert response.content == "final answer"
    assert provider.call_count == 2
    # History should have user + assistant
    assert agent.history[-1]["content"] == "final answer"


async def test_agent_passes_tool_results_in_messages(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)

    await agent.chat("do something")

    messages = provider.last_messages
    # Should have: system, user, assistant(tool_calls), tool, assistant(final)
    roles = [m["role"] for m in messages]
    assert "tool" in roles
    tool_msg = [m for m in messages if m["role"] == "tool"][0]
    # The tool execution goes through the (empty) registry, so we get an error
    assert "Error" in tool_msg["content"] or tool_msg["content"]


async def test_agent_multiple_tool_calls_per_round(config):
    provider = MultiToolProvider()
    agent = make_agent(config, provider)

    response = await agent.chat("multi")

    assert response.content == "done"
    assert provider.call_count == 3
    # The turn's tool calls and results stay in history, bracketed by the
    # question and the answer.
    assert agent.history[0] == {"role": "user", "content": "multi"}
    assert agent.history[-1] == {"role": "assistant", "content": "done"}
    assert any(entry["role"] == "tool" for entry in agent.history)


async def test_agent_rolls_back_on_error(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)

    # Replace with a provider that always fails
    agent.provider = type(provider)()
    agent.provider.call_count = 0

    async def failing_chat(*args, **kwargs):
        raise ProviderError("boom")

    agent.provider.chat_completion = failing_chat

    with pytest.raises(ProviderError):
        await agent.chat("fail")

    assert agent.history == []


async def test_agent_tool_error_does_not_crash(config):
    """Tool returns error string but agent continues."""
    provider = FailToolProvider()
    agent = make_agent(config, provider)

    response = await agent.chat("try reading missing file")

    assert response.content == "recovered"
    # The tool result should be an error message, but agent continues
    messages = provider.last_messages
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "Error" in tool_msgs[0]["content"]


async def test_agent_clear_history(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    await agent.chat("hello")
    assert agent.history
    agent.clear_history()
    assert agent.history == []


async def test_the_next_turn_still_sees_the_last_turn_s_tool_results(config):
    """Otherwise a follow-up question re-reads everything the first one read."""
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    await agent.chat("read something")

    provider.call_count = 0
    await agent.chat("and now?")

    sent = provider.last_messages
    assert any(m["role"] == "tool" for m in sent)


async def test_only_the_newest_turn_keeps_its_tool_results(config):
    """Older rounds collapse back to the conversation, so context stays bounded."""
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    for _ in range(3):
        provider.call_count = 0
        await agent.chat("again")

    assert sum(1 for entry in agent.history if entry["role"] == "tool") == 1


def tool_messages(provider):
    return [m for m in provider.last_messages if m["role"] == "tool"]


async def test_dangerous_tool_denied_without_callback(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    response = await agent.chat("run something")

    assert response.content == "final answer"
    assert tool_messages(provider)[0]["content"] == PERMISSION_DENIED


async def test_dangerous_tool_runs_when_approved(config, tmp_path):
    target = tmp_path / "x.txt"
    provider = ToolCallingProvider(path=str(target))
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    approvals = []

    async def approve(name, args):
        approvals.append((name, args))
        return True

    agent.approve = approve
    await agent.chat("run something")

    assert approvals == [("write_file", {"path": str(target), "content": "hi"})]
    assert "Wrote" in tool_messages(provider)[0]["content"]
    assert target.read_text() == "hi"


async def test_dangerous_tool_denied_by_callback(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async def deny(name, args):
        return False

    agent.approve = deny
    await agent.chat("run something")

    assert tool_messages(provider)[0]["content"] == PERMISSION_DENIED


async def test_a_correction_is_not_read_as_a_refusal(config):
    """A policy describing a mistake needs the opposite answer to a person
    saying no: the model has to make the call again, differently."""
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async def correct(name, args):
        return Correction("Only files under /checkout can be edited.")

    agent.approve = correct
    await agent.chat("run something")

    result = tool_messages(provider)[0]["content"]
    assert "Only files under /checkout" in result
    assert "was not refused" in result
    assert "Do not retry" not in result


async def test_a_plain_string_denial_still_says_not_to_retry(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async def deny(name, args):
        return "I do not want that file touched."

    agent.approve = deny
    await agent.chat("run something")

    result = tool_messages(provider)[0]["content"]
    assert "Do not retry" in result
    assert "I do not want that file touched." in result


async def test_safe_tool_skips_approval(config):
    provider = FailToolProvider()  # calls read_file
    agent = make_agent(config, provider)
    agent.registry = default_registry()

    async def approve(name, args):
        raise AssertionError("approval should not be requested for safe tools")

    agent.approve = approve
    response = await agent.chat("read a file")

    assert response.content == "recovered"
    assert "Error" in tool_messages(provider)[0]["content"]


async def test_chat_stream_denies_dangerous_tool_without_callback(config):
    provider = ToolCallingProvider(reply="adjusted")
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async for _ in agent.chat_stream("run something"):
        pass

    assert tool_messages(provider)[0]["content"] == PERMISSION_DENIED
    assert agent.history[-1]["content"] == "adjusted"


async def test_chat_stream_executes_tools(config):
    provider = ToolCallingProvider(reply="streamed answer")
    agent = make_agent(config, provider)

    chunks = [chunk async for chunk in agent.chat_stream("do something")]

    assert provider.call_count == 2

    # Tool progress is surfaced as tool chunks
    progress = [c for c in chunks if c[0] == "tool" and "🔧" in c[1]]
    assert len(progress) == 1
    assert "write_file" in progress[0][1]

    content_chunks = [c for c in chunks if c[0] == "content"]
    assert "".join(c[1] for c in content_chunks) == "streamed answer"

    assert agent.history[-1]["content"] == "streamed answer"


class TwoReadToolProvider(StreamsFromChatMixin):
    """Requests two read-only tool calls in one round, then finishes."""

    def __init__(self):
        self.call_count = 0
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.call_count += 1
        self.last_messages = messages
        if self.call_count == 1:
            return CompletionResponse(
                content="",
                tool_calls=[
                    {"id": "c1", "type": "function",
                     "function": {"name": "read_a", "arguments": "{}"}},
                    {"id": "c2", "type": "function",
                     "function": {"name": "read_b", "arguments": "{}"}},
                ],
            )
        return CompletionResponse(content="done")

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


def readonly_tool(name, executor):
    return Tool(ToolDefinition(name, "", json_schema({})), executor)


async def test_read_only_tools_run_concurrently(config):
    """Two safe tools in one round overlap; if they ran serially the first
    would deadlock waiting on the second, so completing proves concurrency."""
    b_started = asyncio.Event()

    async def read_a(**kwargs):
        await asyncio.wait_for(b_started.wait(), timeout=1)
        return "a-done"

    async def read_b(**kwargs):
        b_started.set()
        return "b-done"

    provider = TwoReadToolProvider()
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([
        readonly_tool("read_a", read_a),
        readonly_tool("read_b", read_b),
    ])

    response = await agent.chat("read both")

    assert response.content == "done"
    tool_msgs = [m for m in provider.last_messages if m["role"] == "tool"]
    contents = {m["content"] for m in tool_msgs}
    assert contents == {"a-done", "b-done"}  # both completed, no deadlock


async def test_chat_stream_multiple_tool_rounds(config):
    provider = MultiToolProvider()
    agent = make_agent(config, provider)

    chunks = [chunk async for chunk in agent.chat_stream("multi")]

    assert provider.call_count == 3
    progress = [c for c in chunks if c[0] == "tool" and "🔧" in c[1]]
    assert len(progress) == 3
    assert agent.history[-1]["content"] == "done"

    # Tool results were fed back to the provider
    tool_msgs = [m for m in provider.last_messages if m["role"] == "tool"]
    assert len(tool_msgs) == 3


class UsagePerRoundProvider(StreamsFromChatMixin):
    """Reports token usage on every round, tool rounds included."""

    def __init__(self, rounds=3):
        self.rounds = rounds
        self.call_count = 0

    async def chat_completion(self, messages, **kwargs):
        self.call_count += 1
        usage = {"prompt_tokens": 100, "completion_tokens": 10}
        if self.call_count < self.rounds:
            return CompletionResponse(
                content="",
                usage=usage,
                model="deepseek-v4-flash",
                tool_calls=[
                    {
                        "id": f"call_{self.call_count}",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": f"{self.call_count}.txt", "content": "x"}
                            ),
                        },
                    }
                ],
            )
        return CompletionResponse(content="done", usage=usage, model="deepseek-v4-flash")

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


async def test_usage_sums_every_round_not_just_the_last(config):
    """Each tool round is a billed request; the spend cap reads these numbers."""
    provider = UsagePerRoundProvider(rounds=3)
    agent = make_agent(config, provider)

    response = await agent.chat("investigate")

    assert provider.call_count == 3
    assert response.usage == {"prompt_tokens": 300, "completion_tokens": 30}


async def test_usage_is_none_when_the_provider_reports_none(config):
    provider = ToolCallingProvider()
    agent = make_agent(config, provider)

    response = await agent.chat("do something")

    assert response.usage is None


# ---------------------------------------------------------------------------
# One run's spend ceiling
# ---------------------------------------------------------------------------


class ExpensiveProvider(StreamsFromChatMixin):
    """Calls a tool every round, and bills for it."""

    model = "claude-opus-5"

    def __init__(self, per_round=60_000):
        self.calls = 0
        self.per_round = per_round
        self.tools_offered = []
        self.last_messages = None

    async def chat_completion(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.last_messages = messages
        self.tools_offered.append(list(tools or []))
        usage = {"prompt_tokens": self.per_round, "completion_tokens": 1_000}
        if not tools:
            # No tools offered: this is the report round.
            return CompletionResponse(content="the report", usage=usage)
        return CompletionResponse(
            content="",
            usage=usage,
            tool_calls=[
                {
                    "id": f"call_{self.calls}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
                }
            ],
        )

    async def aclose(self):
        pass


def expensive_agent(monkeypatch, limit):
    provider = ExpensiveProvider()
    monkeypatch.setattr(ProviderFactory, "create_provider", lambda *a, **k: provider)
    agent = Agent(
        Config(ai=AIProviderConfig(provider="anthropic", api_key="x")),
        cost_limit_usd=limit,
    )
    return agent, provider


async def test_a_run_that_spends_its_allowance_is_asked_for_the_report(monkeypatch):
    """max_rounds bounds how many requests a run makes, not what they cost.
    Past the ceiling the tools are withheld and the work already paid for is
    banked as a report."""
    agent, provider = expensive_agent(monkeypatch, limit=0.5)

    response = await agent.chat("investigate")

    assert response.content == "the report"
    # $0.325 a round on opus pricing: one round fits, two clear the ceiling,
    # and the third is the report.
    assert provider.calls == 3
    assert provider.tools_offered[-1] == []
    assert agent.spent_usd() > 0.5


async def test_the_last_round_asks_for_the_pending_edit_as_a_diff(monkeypatch):
    """The tools are gone, so an edit the run had not made yet can only land
    as a patch in the report — which costs no extra round and which
    apply_reported_diff applies verbatim. Described, it lands nothing."""
    agent, provider = expensive_agent(monkeypatch, limit=0.5)

    await agent.chat("investigate")

    instruction = "\n".join(
        m["content"] for m in provider.last_messages if m["role"] == "user"
    )
    assert "unified diff" in instruction
    assert "--- a/" in instruction and "@@" in instruction


async def test_a_run_inside_its_allowance_is_left_alone(monkeypatch):
    agent, provider = expensive_agent(monkeypatch, limit=100.0)

    await agent.chat("investigate")

    assert all(offered for offered in provider.tools_offered)


async def test_no_ceiling_means_no_ceiling(monkeypatch):
    """0 is documented as "no cap", and chat sets no limit at all."""
    agent, provider = expensive_agent(monkeypatch, limit=0.0)

    await agent.chat("investigate")

    assert provider.calls == agent.max_rounds
    assert all(offered for offered in provider.tools_offered)


async def test_spend_accumulates_across_a_run_not_just_a_turn(monkeypatch):
    """One incident is several turns, and the ceiling is for the incident."""
    agent, provider = expensive_agent(monkeypatch, limit=100.0)

    await agent.chat("investigate")
    first = agent.spent_usd()
    agent.take_usage()  # the daemon banks each turn's usage as it goes
    await agent.chat("again")

    assert agent.spent_usd() > first


# ---------------------------------------------------------------------------
# The output-token ceiling
# ---------------------------------------------------------------------------


class TruncatingProvider(StreamsFromChatMixin):
    """Stops mid-sentence for its first `cuts` answers, then finishes."""

    def __init__(self, cuts=1):
        self.cuts = cuts
        self.calls = 0
        self.prompts = []
        self.usage = None

    async def chat_completion(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        cut = self.calls <= self.cuts
        return CompletionResponse(
            content=(
                f"## Applied fix\npart {self.calls} of it, cut off mid-"
                if cut else "sentence. Done."
            ),
            finish_reason="length" if cut else "stop",
            usage=self.usage,
        )

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


async def test_an_answer_cut_off_by_the_output_ceiling_is_continued(config):
    """A report that stops mid-sentence passes every check the daemon makes —
    it is long enough, it has its headings — and is filed half written. In fix
    mode the tokens it ran out of are the ones the edit needed."""
    provider = TruncatingProvider()
    agent = make_agent(config, provider)

    response = await agent.chat("investigate")

    assert response.content == (
        "## Applied fix\npart 1 of it, cut off mid-sentence. Done."
    )
    assert provider.calls == 2
    assert "continue it" in provider.prompts[1].lower()


async def test_a_complete_answer_buys_no_continuation(config):
    provider = TruncatingProvider(cuts=0)
    agent = make_agent(config, provider)

    await agent.chat("investigate")

    assert provider.calls == 1


async def test_continuations_are_bounded(config):
    """Then the partial answer is filed as it stands: a model still going
    after two continuations is not writing a report."""
    provider = TruncatingProvider(cuts=99)
    agent = make_agent(config, provider)

    response = await agent.chat("investigate")

    assert provider.calls == 1 + MAX_CONTINUATIONS
    assert response.content.endswith("cut off mid-")


async def test_a_continuation_is_billed_to_the_run(config):
    """It is a request like any other, and the spend cap reads these."""
    provider = TruncatingProvider()
    provider.usage = {"prompt_tokens": 100, "completion_tokens": 10}
    agent = make_agent(config, provider)

    response = await agent.chat("investigate")

    assert response.usage == {"prompt_tokens": 200, "completion_tokens": 20}


async def test_the_continuation_is_asked_with_no_tools(config):
    """This branch was reached because the model was answering rather than
    calling, and a tool call mid-report restarts the answer."""
    offered = []

    provider = TruncatingProvider()
    inner = provider.chat_completion

    async def record(messages, tools=None, **kwargs):
        offered.append(list(tools or []))
        return await inner(messages, tools, **kwargs)

    provider.chat_completion = record
    agent = make_agent(config, provider)
    agent.registry = default_registry()

    await agent.chat("investigate")

    assert offered[0] and offered[1] == []


class TruncatedCallProvider(StreamsFromChatMixin):
    """A write_file whose long `content` argument was cut off mid-JSON."""

    def __init__(self):
        self.calls = 0
        self.last_messages = None

    async def chat_completion(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.last_messages = messages
        if self.calls == 1:
            return CompletionResponse(
                content="",
                finish_reason="length",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "x.txt", "content": "the who',
                    },
                }],
            )
        return CompletionResponse(content="final answer")

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


async def test_a_call_whose_arguments_were_cut_off_is_not_a_refusal(config):
    """The ceiling truncates the arguments of a long write_file, and the
    unparseable result used to reach the permission gate as a call with no
    path at all. Being told the user refused an edit — and not to retry it —
    is how fix mode ended a run having changed nothing."""
    provider = TruncatedCallProvider()
    agent = make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async def approve(name, args):
        raise AssertionError("a call that never parsed is not the gate's to judge")

    agent.approve = approve
    await agent.chat("fix it")

    result = tool_messages(provider)[0]["content"]
    assert "not valid JSON" in result
    assert PERMISSION_DENIED not in result
