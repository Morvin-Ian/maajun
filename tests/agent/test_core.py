import pytest

from maajun.agent.core import (
    MAX_HISTORY_MESSAGES,
    MAX_REQUEST_CHARS,
    TRIM_TARGET_CHARS,
    Agent,
    trim_request_messages,
)
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import CompletionResponse, ProviderError
from maajun.providers.factory import ProviderFactory


class FakeProvider:
    def __init__(self, reply="pong", fail=False):
        self.reply = reply
        self.fail = fail
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.last_messages = list(messages)
        if self.fail:
            raise ProviderError("boom")
        return CompletionResponse(content=self.reply, thinking="hmm ")

    async def stream_completion(self, messages, **kwargs):
        self.last_messages = list(messages)
        if self.fail:
            raise ProviderError("boom")
        yield "thinking", "hmm "
        yield "content", self.reply[: len(self.reply) // 2]
        yield "content", self.reply[len(self.reply) // 2 :]


@pytest.fixture
def agent(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(ProviderFactory, "create_provider", lambda *a, **k: fake)
    return Agent(Config(ai=AIProviderConfig(provider="deepseek", api_key="x")))


async def test_chat_appends_both_turns(agent):
    response = await agent.chat("ping")
    assert response.content == "pong"
    assert agent.history == [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]


async def test_chat_rolls_back_user_message_on_error(agent):
    agent.provider.fail = True
    with pytest.raises(ProviderError):
        await agent.chat("ping")
    assert agent.history == []


async def test_chat_stream_accumulates_content_into_history(agent):
    chunks = [chunk async for chunk in agent.chat_stream("ping")]
    assert ("thinking", "hmm ") in chunks
    assert agent.history[-1] == {"role": "assistant", "content": "pong"}


async def test_chat_stream_rolls_back_on_error(agent):
    agent.provider.fail = True
    with pytest.raises(ProviderError):
        async for _ in agent.chat_stream("ping"):
            pass
    assert agent.history == []


async def test_request_window_is_capped(agent):
    for i in range(MAX_HISTORY_MESSAGES * 2):
        agent.history.append({"role": "user", "content": str(i)})
    await agent.chat("latest")
    # system prompt + at most MAX_HISTORY_MESSAGES history entries
    assert len(agent.provider.last_messages) == MAX_HISTORY_MESSAGES + 1
    assert agent.provider.last_messages[0]["role"] == "system"
    assert agent.provider.last_messages[-1]["content"] == "latest"


# ---------------------------------------------------------------------------
# Chat UI: /history replay
# ---------------------------------------------------------------------------


def test_base_url_reaches_the_provider(monkeypatch):
    """`ai.base_url` is documented as the gateway switch — it must be wired."""
    captured = {}

    def fake_create(provider_type, provider_config):
        captured.update(provider_config)
        return object()

    monkeypatch.setattr(ProviderFactory, "create_provider", fake_create)
    config = Config(
        ai=AIProviderConfig(
            provider="deepseek", api_key="x", base_url="https://gateway.internal/v1"
        )
    )

    Agent(config)

    assert captured["base_url"] == "https://gateway.internal/v1"


# ---------------------------------------------------------------------------
# request trimming
# ---------------------------------------------------------------------------


def msg(role, content, tool_calls=None):
    message = {"role": role, "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def test_trim_leaves_a_small_request_untouched():
    messages = [msg("system", "sys"), msg("user", "hi"), msg("assistant", "yo")]
    before = list(messages)
    trim_request_messages(messages)
    assert messages == before


def test_trim_drops_oldest_but_keeps_the_system_prompt():
    filler = "z" * 50_000
    messages = [msg("system", "sys")] + [
        msg("user" if i % 2 == 0 else "assistant", filler) for i in range(10)
    ]
    trim_request_messages(messages)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "sys"
    assert sum(len(m["content"]) for m in messages) <= MAX_REQUEST_CHARS
    # The most recent turn survives; the oldest ones are the ones dropped.
    assert messages[-1]["content"] == filler


def test_trim_never_leaves_an_orphan_tool_result_at_the_front():
    """A tool message with no preceding tool_call is rejected by the API."""
    filler = "z" * 40_000
    calls = [{"id": "c1", "function": {"name": "grep", "arguments": "{}"}}]
    messages = [msg("system", "sys")]
    for _ in range(6):
        messages.append(msg("user", filler))
        messages.append(msg("assistant", "", calls))
        messages.append(msg("tool", filler))
    trim_request_messages(messages)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] != "tool"


def test_trim_drops_below_the_floor_rather_than_orphan_a_tool_result():
    """The floor is MIN_REQUEST_MESSAGES, but a request the API rejects
    outright is worse than a short one — so the floor is what gives way.

    Two write_file calls carrying large arguments: each assistant message is
    over the budget on its own, and cap_result does not bound tool_call
    arguments the way it bounds results.
    """
    big = "x" * 90_000

    def call(cid):
        return [{"id": cid, "function": {"name": "write_file", "arguments": big}}]

    messages = [
        msg("system", "sys"),
        msg("assistant", "", call("c1")),
        msg("tool", "Wrote"),
        msg("assistant", "", call("c2")),
        msg("tool", "Wrote"),
    ]
    trim_request_messages(messages)
    assert messages[0]["role"] == "system"
    assert [m["role"] for m in messages[1:]] != ["tool", "assistant", "tool"]
    assert all(
        not (m["role"] == "tool" and messages[i - 1].get("role") == "system")
        for i, m in enumerate(messages)
        if i
    )


def test_trim_keeps_a_floor_even_when_one_message_blows_the_budget():
    huge = "z" * (MAX_REQUEST_CHARS * 3)
    messages = [msg("system", "sys"), msg("user", "q"), msg("assistant", huge)]
    trim_request_messages(messages)
    assert len(messages) == 3
    assert messages[1]["content"] == "q"


def test_trim_counts_tool_call_arguments_toward_the_budget():
    calls = [{"id": "c1", "function": {"name": "grep", "arguments": "a" * 90_000}}]
    messages = [
        msg("system", "sys"),
        msg("user", "q"),
        msg("assistant", "", calls),
        msg("assistant", "", calls),
        msg("assistant", "tail"),
    ]
    trim_request_messages(messages)
    assert len(messages) < 5


def test_trim_cuts_back_past_the_ceiling_not_just_under_it():
    """Trimming to the line means the next round pushes over it again and
    drops one more message, so the provider's cached prefix is invalidated
    every round. One deeper cut keeps it stable for several rounds."""
    filler = "z" * 20_000
    messages = [msg("system", "sys")] + [
        msg("user" if i % 2 == 0 else "assistant", filler) for i in range(12)
    ]
    trim_request_messages(messages)
    assert sum(len(m["content"]) for m in messages) <= TRIM_TARGET_CHARS


def test_a_request_between_the_target_and_the_ceiling_is_left_alone():
    """Only crossing MAX_REQUEST_CHARS trims; below it the prefix is kept
    byte-identical so the cache still hits."""
    filler = "z" * 10_000
    messages = [msg("system", "sys")] + [msg("user", filler) for _ in range(14)]
    assert TRIM_TARGET_CHARS < 140_000 <= MAX_REQUEST_CHARS
    before = list(messages)
    trim_request_messages(messages)
    assert messages == before
