from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, RateLimitError

from maajun.providers.base import ProviderError
from maajun.providers.chat_completions import MAX_RETRIES
from maajun.providers.deepseek import DeepSeekProvider

DEFAULT_MODEL = DeepSeekProvider.default_model
THINKING_MODEL = DeepSeekProvider.thinking_model


def _strip_dsml(text: str) -> str:
    """DSML stripping is now a provider method, not a module function."""
    return DeepSeekProvider({"api_key": "k"}).clean_content(text)


def _fake_httpx_response(status_code):
    """Build a minimal httpx.Response-like object for openai exceptions."""
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    return httpx.Response(status_code, request=request)


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


def test_custom_model_override():
    provider = DeepSeekProvider({"api_key": "x", "model": "deepseek-v4-pro"})
    assert provider.model == "deepseek-v4-pro"


def test_thinking_mode_does_not_override_custom_model():
    """Naming a model is specific; thinking_mode is only a shorthand.

    This test's name always said so, but it used to assert the opposite with
    a comment conceding the behavior was wrong.
    """
    provider = DeepSeekProvider({"api_key": "x", "model": "my-custom", "thinking_mode": True})
    assert provider.model == "my-custom"


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


def make_stream_chunk(content=None, reasoning=None, tool_calls=None):
    delta = SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=tool_calls
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def make_tool_call_delta(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def make_streaming_provider(chunks):
    provider = DeepSeekProvider({"api_key": "x"})

    async def fake_stream():
        for chunk in chunks:
            yield chunk

    class FakeCompletions:
        async def create(self, **kwargs):
            return fake_stream()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    return provider


async def test_stream_completion_yields_text_deltas():
    provider = make_streaming_provider([
        make_stream_chunk(reasoning="think"),
        make_stream_chunk(content="hel"),
        make_stream_chunk(content="lo"),
    ])
    events = [e async for e in provider.stream_completion(messages=[])]
    assert events == [("thinking", "think"), ("content", "hel"), ("content", "lo")]


async def test_stream_completion_accumulates_tool_calls():
    provider = make_streaming_provider([
        make_stream_chunk(content="on it"),
        make_stream_chunk(tool_calls=[
            make_tool_call_delta(0, id="call_1", name="bash", arguments='{"comm'),
        ]),
        make_stream_chunk(tool_calls=[
            make_tool_call_delta(0, arguments='and": "ls"}'),
        ]),
        make_stream_chunk(tool_calls=[
            make_tool_call_delta(1, id="call_2", name="read_file", arguments="{}"),
        ]),
    ])
    events = [e async for e in provider.stream_completion(messages=[])]

    assert events[-1][0] == "tool_calls"
    assert events[-1][1] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        },
    ]


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


def _make_rate_limit_error():
    """Create a RateLimitError that the openai SDK would raise on 429."""
    return RateLimitError(
        message="rate limit exceeded",
        response=_fake_httpx_response(429),
        body=None,
    )


def _make_api_status_error(status_code):
    """Create an APIStatusError for the given HTTP status."""
    return APIStatusError(
        message=f"error {status_code}",
        response=_fake_httpx_response(status_code),
        body=None,
    )


def _fail_then_succeed(failures, final_result):
    """Return an async callable that raises failures then returns final_result."""
    call_count = 0

    async def _call(**kwargs):
        nonlocal call_count
        if call_count < len(failures):
            exc = failures[call_count]
            call_count += 1
            raise exc
        call_count += 1
        return final_result

    return _call


@pytest.mark.asyncio
async def test_retry_succeeds_after_rate_limit(monkeypatch):
    """Retries on 429 and eventually succeeds."""
    async def _no_sleep(_delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", _no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_fail_then_succeed(
            [_make_rate_limit_error(), _make_rate_limit_error()],
            make_response(content="ok"),
        )))
    )
    result = await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_retries(monkeypatch):
    """Exhausts retries and raises ProviderError."""
    async def _no_sleep(_delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", _no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    errors = [_make_rate_limit_error() for _ in range(MAX_RETRIES + 1)]
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_fail_then_succeed(
            errors, make_response(),
        )))
    )
    with pytest.raises(ProviderError, match="Rate limit"):
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_retry_on_server_error(monkeypatch):
    """Retries on 500 and succeeds."""
    async def _no_sleep(_delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", _no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_fail_then_succeed(
            [_make_api_status_error(500)],
            make_response(content="recovered"),
        )))
    )
    result = await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "recovered"


@pytest.mark.asyncio
async def test_no_retry_on_auth_error(monkeypatch):
    """Auth errors (401) are not retried."""
    async def _no_sleep(_delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", _no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    call_count = 0

    async def auth_fail(**kwargs):
        nonlocal call_count
        call_count += 1
        # 401 is not in the retryable set (only 429, 500, 502, 503)
        raise _make_api_status_error(401)

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=auth_fail))
    )
    with pytest.raises(ProviderError):
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert call_count == 1  # no retries


def test_prepared_tools_are_rebuilt_every_call():
    """No memoization: an id()-keyed cache can serve a freed list's entry."""
    from maajun.providers.base import ToolDefinition

    provider = DeepSeekProvider({"api_key": "x"})
    tools = [ToolDefinition("t", "d", {"type": "object", "properties": {}})]

    first = provider._prepared_tools(tools)
    again = provider._prepared_tools(tools)
    assert first == again
    assert first is not again

    assert provider._prepared_tools(None) is None


def test_prepared_tools_reflect_the_list_they_were_given():
    """The regression the id() cache allowed: same address, different tools."""
    from maajun.providers.base import ToolDefinition

    provider = DeepSeekProvider({"api_key": "x"})
    schema = {"type": "object", "properties": {}}

    read = provider._prepared_tools([ToolDefinition("read_file", "d", schema)])
    grep = provider._prepared_tools([ToolDefinition("grep", "d", schema)])
    assert read[0]["function"]["name"] == "read_file"
    assert grep[0]["function"]["name"] == "grep"


async def test_aclose_is_safe_without_client():
    provider = DeepSeekProvider({"api_key": "x"})
    await provider.aclose()  # never initialized -> no error
    assert provider.client is None
