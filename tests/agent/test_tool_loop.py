"""Tests for the agent tool-calling loop."""

import asyncio
import json

import pytest

from maajun.agent.core import PERMISSION_DENIED, Agent
from maajun.agent.tools import WRITE_FILE, ToolRegistry, default_registry
from maajun.agent.tools.base import Tool, json_schema
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import CompletionResponse, ProviderError, ToolDefinition

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
def _isolate_cwd(monkeypatch, tmp_path):
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


def _make_agent(config, provider):
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
    agent = _make_agent(config, provider)

    response = await agent.chat("do something")

    assert response.content == "final answer"
    assert provider.call_count == 2
    # History should have user + assistant
    assert agent.history[-1]["content"] == "final answer"


async def test_agent_passes_tool_results_in_messages(config):
    provider = ToolCallingProvider()
    agent = _make_agent(config, provider)

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
    agent = _make_agent(config, provider)

    response = await agent.chat("multi")

    assert response.content == "done"
    assert provider.call_count == 3
    # History should have user + assistant
    assert len(agent.history) == 2


async def test_agent_rolls_back_on_error(config):
    provider = ToolCallingProvider()
    agent = _make_agent(config, provider)

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
    agent = _make_agent(config, provider)

    response = await agent.chat("try reading missing file")

    assert response.content == "recovered"
    # The tool result should be an error message, but agent continues
    messages = provider.last_messages
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "Error" in tool_msgs[0]["content"]


async def test_agent_clear_history(config):
    provider = ToolCallingProvider()
    agent = _make_agent(config, provider)
    await agent.chat("hello")
    assert len(agent.history) == 2
    agent.clear_history()
    assert agent.history == []


def _tool_messages(provider):
    return [m for m in provider.last_messages if m["role"] == "tool"]


async def test_dangerous_tool_denied_without_callback(config):
    provider = ToolCallingProvider()
    agent = _make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    response = await agent.chat("run something")

    assert response.content == "final answer"
    assert _tool_messages(provider)[0]["content"] == PERMISSION_DENIED


async def test_dangerous_tool_runs_when_approved(config, tmp_path):
    target = tmp_path / "x.txt"
    provider = ToolCallingProvider(path=str(target))
    agent = _make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    approvals = []

    async def approve(name, args):
        approvals.append((name, args))
        return True

    agent.approve = approve
    await agent.chat("run something")

    assert approvals == [("write_file", {"path": str(target), "content": "hi"})]
    assert "Wrote" in _tool_messages(provider)[0]["content"]
    assert target.read_text() == "hi"


async def test_dangerous_tool_denied_by_callback(config):
    provider = ToolCallingProvider()
    agent = _make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async def deny(name, args):
        return False

    agent.approve = deny
    await agent.chat("run something")

    assert _tool_messages(provider)[0]["content"] == PERMISSION_DENIED


async def test_safe_tool_skips_approval(config):
    provider = FailToolProvider()  # calls read_file
    agent = _make_agent(config, provider)
    agent.registry = default_registry()

    async def approve(name, args):
        raise AssertionError("approval should not be requested for safe tools")

    agent.approve = approve
    response = await agent.chat("read a file")

    assert response.content == "recovered"
    assert "Error" in _tool_messages(provider)[0]["content"]


async def test_chat_stream_denies_dangerous_tool_without_callback(config):
    provider = ToolCallingProvider(reply="adjusted")
    agent = _make_agent(config, provider)
    agent.registry = ToolRegistry([WRITE_FILE])

    async for _ in agent.chat_stream("run something"):
        pass

    assert _tool_messages(provider)[0]["content"] == PERMISSION_DENIED
    assert agent.history[-1]["content"] == "adjusted"


async def test_chat_stream_executes_tools(config):
    provider = ToolCallingProvider(reply="streamed answer")
    agent = _make_agent(config, provider)

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


def _readonly_tool(name, executor):
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
    agent = _make_agent(config, provider)
    agent.registry = ToolRegistry([
        _readonly_tool("read_a", read_a),
        _readonly_tool("read_b", read_b),
    ])

    response = await agent.chat("read both")

    assert response.content == "done"
    tool_msgs = [m for m in provider.last_messages if m["role"] == "tool"]
    contents = {m["content"] for m in tool_msgs}
    assert contents == {"a-done", "b-done"}  # both completed, no deadlock


async def test_chat_stream_multiple_tool_rounds(config):
    provider = MultiToolProvider()
    agent = _make_agent(config, provider)

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
    agent = _make_agent(config, provider)

    response = await agent.chat("investigate")

    assert provider.call_count == 3
    assert response.usage == {"prompt_tokens": 300, "completion_tokens": 30}


async def test_usage_is_none_when_the_provider_reports_none(config):
    provider = ToolCallingProvider()
    agent = _make_agent(config, provider)

    response = await agent.chat("do something")

    assert response.usage is None
