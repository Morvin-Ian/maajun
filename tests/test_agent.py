import pytest

from maajun.agent.core import MAX_HISTORY_MESSAGES, Agent
from maajun.config import AIProviderConfig, Config
from maajun.providers.base import CompletionResponse, ProviderError
from maajun.providers.factory import ProviderFactory


class FakeProvider:
    def __init__(self, reply="pong", fail=False):
        self.reply = reply
        self.fail = fail
        self.last_messages = None

    async def chat_completion(self, messages, **kwargs):
        self.last_messages = messages
        if self.fail:
            raise ProviderError("boom")
        return CompletionResponse(content=self.reply)

    async def stream_completion(self, messages, **kwargs):
        self.last_messages = messages
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
