from types import SimpleNamespace

from maajun.providers.deepseek import (
    DEFAULT_MODEL,
    THINKING_MODEL,
    DeepSeekProvider,
    _strip_dsml,
)


def make_response(content="hello", reasoning=None, tool_calls=None):
    message = SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=reasoning
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_strip_dsml_removes_blocks():
    text = "before <|DSML|>secret</|DSML|tool_calls> after"
    assert _strip_dsml(text) == "before  after".strip()


def test_strip_dsml_removes_dangling_open_tag():
    assert _strip_dsml("hi <|DSML|foo>") == "hi"


def test_strip_dsml_leaves_plain_text():
    assert _strip_dsml("plain answer") == "plain answer"


def test_default_model():
    provider = DeepSeekProvider({"api_key": "x"})
    assert provider.model == DEFAULT_MODEL


def test_thinking_mode_switches_model():
    provider = DeepSeekProvider({"api_key": "x", "thinking_mode": True})
    assert provider.model == THINKING_MODEL


def test_parse_response_basic():
    provider = DeepSeekProvider({"api_key": "x"})
    result = provider.parse_response(make_response(content="hi", reasoning="hmm"))
    assert result.content == "hi"
    assert result.thinking == "hmm"
    assert result.usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def test_parse_response_strips_dsml():
    provider = DeepSeekProvider({"api_key": "x"})
    result = provider.parse_response(make_response(content="ok <|DSML|x>"))
    assert result.content == "ok"
