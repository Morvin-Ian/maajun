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
        return CompletionResponse(content=self.reply, thinking="hmm ")

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


# ---------------------------------------------------------------------------
# Chat UI: /history replay
# ---------------------------------------------------------------------------


async def test_history_command_replays_both_roles(agent, capsys):
    """Regression guard: this path had no coverage, so a rename that left an
    undefined name in it was caught only by the linter."""
    from rich.console import Console

    from maajun.chat_ui import _chat_loop

    await agent.chat("ping")

    console = Console(force_terminal=False, width=80)
    prompts = iter(["/history", "/quit"])

    async def fake_prompt(session, text):
        return next(prompts)

    import maajun.chat_ui as chat_ui

    original = chat_ui._prompt
    chat_ui._prompt = fake_prompt
    try:
        await _chat_loop(agent, console)
    finally:
        chat_ui._prompt = original

    output = capsys.readouterr().out
    assert "ping" in output
    assert "pong" in output


def test_rendered_markdown_keeps_code_blocks_unpadded():
    """Rich pads code blocks by a column, which breaks copied commands."""
    from rich.console import Console

    from maajun.chat_ui import _rendered

    console = Console(force_terminal=False, width=60, file=None)
    with console.capture() as capture:
        console.print(_rendered("Run:\n\n```bash\nmaajun watch --once\n```\n"))
    lines = [line for line in capture.get().splitlines() if "maajun watch" in line]
    assert lines and not lines[0].startswith(" ")
