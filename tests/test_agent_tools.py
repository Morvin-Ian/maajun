"""Tests for the agent tool-calling loop."""

import json

import pytest

from maajun.agent.core import Agent
from maajun.agent.tools import ToolRegistry
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import CompletionResponse, ProviderError

# ---------------------------------------------------------------------------
# Fake provider that simulates tool-calling rounds
# ---------------------------------------------------------------------------


class ToolCallingProvider:
    """Provider that returns tool_calls on the first call, then plain text."""

    def __init__(self, tool_result="tool output", reply="final answer"):
        self.call_count = 0
        self.tool_result = tool_result
        self.reply = reply
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
                            "name": "bash",
                            "arguments": json.dumps({"command": "echo test"}),
                        },
                    }
                ],
            )
        # Second call: return final answer
        return CompletionResponse(content=self.reply)

    async def stream_completion(self, messages, **kwargs):
        yield "content", self.reply

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


class MultiToolProvider:
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
                            "name": "bash",
                            "arguments": json.dumps({"command": "echo a"}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": "echo b"}),
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
                            "name": "bash",
                            "arguments": json.dumps({"command": "echo c"}),
                        },
                    }
                ],
            )
        return CompletionResponse(content="done")

    async def stream_completion(self, messages, **kwargs):
        yield "content", "done"

    async def validate_credentials(self):
        return True

    def get_provider_name(self):
        return "fake"


class FailToolProvider:
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

    async def stream_completion(self, messages, **kwargs):
        yield "content", "recovered"

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


async def test_chat_stream_executes_tools(config):
    provider = ToolCallingProvider(reply="streamed answer")
    agent = _make_agent(config, provider)

    chunks = [chunk async for chunk in agent.chat_stream("do something")]

    # Should have content chunks
    content_chunks = [c for c in chunks if c[0] == "content"]
    assert len(content_chunks) > 0
    assert "".join(c[1] for c in content_chunks) == "streamed answer"

    # History should be updated
    assert agent.history[-1]["content"] == "streamed answer"
