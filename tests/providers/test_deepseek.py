from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, RateLimitError

from maajun.providers.base import ProviderError
from maajun.providers.chat_completions import MAX_RETRIES
from maajun.providers.deepseek import DeepSeekProvider

DEFAULT_MODEL = DeepSeekProvider.default_model
THINKING_MODEL = DeepSeekProvider.thinking_model


def strip_dsml(text: str) -> str:
    """DSML stripping is now a provider method, not a module function."""
    return DeepSeekProvider({"api_key": "k"}).clean_content(text)


def fake_httpx_response(status_code):
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
    assert strip_dsml(text) == "before  after".strip()


def test_strip_dsml_removes_dangling_open_tag():
    assert strip_dsml("hi <|DSML|foo>") == "hi"


def test_strip_dsml_leaves_plain_text():
    assert strip_dsml("plain answer") == "plain answer"


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


async def as_awaitable(value):
    """Wrap a value so it can stand in for an awaited open_stream()."""
    return value


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


def make_rate_limit_error():
    """Create a RateLimitError that the openai SDK would raise on 429."""
    return RateLimitError(
        message="rate limit exceeded",
        response=fake_httpx_response(429),
        body=None,
    )


def make_api_status_error(status_code):
    """Create an APIStatusError for the given HTTP status."""
    return APIStatusError(
        message=f"error {status_code}",
        response=fake_httpx_response(status_code),
        body=None,
    )


def fail_then_succeed(failures, final_result):
    """Return an async callable that raises failures then returns final_result."""
    call_count = 0

    async def call(**kwargs):
        nonlocal call_count
        if call_count < len(failures):
            exc = failures[call_count]
            call_count += 1
            raise exc
        call_count += 1
        return final_result

    return call


@pytest.mark.asyncio
async def test_retry_succeeds_after_rate_limit(monkeypatch):
    """Retries on 429 and eventually succeeds."""
    async def no_sleep(delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_then_succeed(
            [make_rate_limit_error(), make_rate_limit_error()],
            make_response(content="ok"),
        )))
    )
    result = await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_retries(monkeypatch):
    """Exhausts retries and raises ProviderError."""
    async def no_sleep(delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    errors = [make_rate_limit_error() for _ in range(MAX_RETRIES + 1)]
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_then_succeed(
            errors, make_response(),
        )))
    )
    with pytest.raises(ProviderError, match="Rate limit"):
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_retry_on_server_error(monkeypatch):
    """Retries on 500 and succeeds."""
    async def no_sleep(delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_then_succeed(
            [make_api_status_error(500)],
            make_response(content="recovered"),
        )))
    )
    result = await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "recovered"


@pytest.mark.asyncio
async def test_no_retry_on_auth_error(monkeypatch):
    """Auth errors (401) are not retried."""
    async def no_sleep(delay):
        pass
    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    call_count = 0

    async def auth_fail(**kwargs):
        nonlocal call_count
        call_count += 1
        # 401 is not in the retryable set (only 429, 500, 502, 503)
        raise make_api_status_error(401)

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=auth_fail))
    )
    with pytest.raises(ProviderError):
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert call_count == 1  # no retries


def test_prepared_tools_are_built_once_per_set():
    """Keyed on the names, not id(): the tool loop asks once per round for
    the same list, and a freed address can be reused."""
    from maajun.providers.base import ToolDefinition

    provider = DeepSeekProvider({"api_key": "x"})
    tools = [ToolDefinition("t", "d", {"type": "object", "properties": {}})]

    first = provider.prepared_tools(tools)
    again = provider.prepared_tools(list(tools))
    assert first is again

    assert provider.prepared_tools(None) is None


def test_prepared_tools_reflect_the_list_they_were_given():
    """The regression the id() cache allowed: same address, different tools."""
    from maajun.providers.base import ToolDefinition

    provider = DeepSeekProvider({"api_key": "x"})
    schema = {"type": "object", "properties": {}}

    read = provider.prepared_tools([ToolDefinition("read_file", "d", schema)])
    grep = provider.prepared_tools([ToolDefinition("grep", "d", schema)])
    assert read[0]["function"]["name"] == "read_file"
    assert grep[0]["function"]["name"] == "grep"


async def test_aclose_is_safe_without_client():
    provider = DeepSeekProvider({"api_key": "x"})
    await provider.aclose()  # never initialized -> no error
    assert provider.client is None


# ---------------------------------------------------------------------------
# validate_credentials
# ---------------------------------------------------------------------------


def capturing_provider(config):
    """A provider whose completions.create records its kwargs and succeeds."""
    provider = DeepSeekProvider(config)
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return make_response()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, seen


async def test_validate_checks_the_configured_model_not_the_default():
    """Regression: setup said the key worked, then every real call 404'd.

    With ai.model set to something the account cannot reach, validating the
    provider default passed while the model the daemon actually sends failed.
    """
    provider, seen = capturing_provider({"api_key": "k", "model": "custom-model"})

    assert await provider.validate_credentials() is True
    assert seen["model"] == "custom-model"


async def test_validate_checks_the_thinking_model_when_that_is_selected():
    provider, seen = capturing_provider({"api_key": "k", "thinking_mode": True})

    await provider.validate_credentials()
    assert seen["model"] == THINKING_MODEL


async def test_validate_falls_back_to_the_default_model():
    provider, seen = capturing_provider({"api_key": "k"})

    await provider.validate_credentials()
    assert seen["model"] == DEFAULT_MODEL


async def test_validate_returns_false_when_the_api_rejects_the_key():
    provider = DeepSeekProvider({"api_key": "k"})

    async def create(**kwargs):
        raise APIStatusError(
            "unauthorized", response=fake_httpx_response(401), body=None
        )

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert await provider.validate_credentials() is False


# ---------------------------------------------------------------------------
# Retry and cleanup
# ---------------------------------------------------------------------------


async def test_the_last_attempt_does_not_sleep_before_giving_up(monkeypatch):
    """Sleeping after the final try made the caller wait up to MAX_DELAY for
    an error it was always going to get."""
    slept = []

    async def record(delay):
        slept.append(delay)

    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", record)
    provider = DeepSeekProvider({"api_key": "x"})
    errors = [make_rate_limit_error() for _ in range(MAX_RETRIES + 1)]
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_then_succeed(
            errors, make_response(),
        )))
    )

    with pytest.raises(ProviderError):
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert len(slept) == MAX_RETRIES - 1, "one sleep between attempts, none after"


async def test_a_non_transient_error_does_not_sleep_at_all(monkeypatch):
    slept = []

    async def record(delay):
        slept.append(delay)

    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", record)
    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_then_succeed(
            [make_api_status_error(400)], make_response(),
        )))
    )

    with pytest.raises(ProviderError):
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert slept == []


async def test_the_provider_error_keeps_the_original_as_its_cause(monkeypatch):
    """The raise sits outside the except block, so without an explicit
    `from` the provider's own traceback is dropped from the chain."""
    async def no_sleep(delay):
        pass

    monkeypatch.setattr("maajun.providers.chat_completions.asyncio.sleep", no_sleep)
    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_then_succeed(
            [make_rate_limit_error() for _ in range(MAX_RETRIES)], make_response(),
        )))
    )

    with pytest.raises(ProviderError) as caught:
        await provider.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert isinstance(caught.value.__cause__, RateLimitError)


async def test_close_quietly_handles_every_shape_of_closer():
    from maajun.providers.chat_completions import close_quietly

    closed = []

    class Async:
        async def close(self):
            closed.append("async")

    class Sync:
        def close(self):
            closed.append("sync")

    class Broken:
        def close(self):
            raise RuntimeError("already gone")

    for stream in (Async(), Sync(), Broken(), object()):
        await close_quietly(stream)

    assert closed == ["async", "sync"]


async def test_a_finished_stream_is_closed():
    """The response holds its connection out of the pool until it is closed,
    and a watch run makes one per tool round."""
    closed = []

    class Stream:
        async def __aiter__(self):
            yield make_stream_chunk(content="hi")

        async def close(self):
            closed.append(True)

    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = object()
    provider.open_stream = lambda **kwargs: as_awaitable(Stream())

    events = [e async for e in provider.stream_completion(messages=[])]

    assert events == [("content", "hi")]
    assert closed == [True]


async def test_an_abandoned_stream_is_still_closed():
    """A caller that stops iterating — or a round that raises — must not
    leak the connection."""
    closed = []

    class Stream:
        async def __aiter__(self):
            for text in ("hel", "lo", "!"):
                yield make_stream_chunk(content=text)

        async def close(self):
            closed.append(True)

    provider = DeepSeekProvider({"api_key": "x"})
    provider.client = object()

    async def open_stream(**kwargs):
        return Stream()

    provider.open_stream = open_stream

    stream = provider.stream_completion(messages=[])
    async for _ in stream:
        break
    await stream.aclose()

    assert closed == [True]


# ---------------------------------------------------------------------------
# Cached prompt tokens
# ---------------------------------------------------------------------------


def test_usage_carries_deepseek_cache_hits():
    """DeepSeek bills a resent prefix at a thirtieth of a fresh one, so the
    hit count has to survive into the usage dict the cost is worked out from."""
    from maajun.providers.chat_completions import usage_of

    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=10,
        total_tokens=1010,
        prompt_cache_hit_tokens=960,
        prompt_cache_miss_tokens=40,
    )
    assert usage_of(usage)["cached_tokens"] == 960


def test_usage_carries_openai_cache_hits():
    """OpenAI nests the same number under prompt_tokens_details."""
    from maajun.providers.chat_completions import usage_of

    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=10,
        total_tokens=1010,
        prompt_tokens_details=SimpleNamespace(cached_tokens=512),
    )
    assert usage_of(usage)["cached_tokens"] == 512


def test_usage_omits_cache_hits_when_the_provider_reports_none():
    """Absent is not zero: a gateway that says nothing must not be recorded
    as having cached nothing, and both are charged in full anyway."""
    from maajun.providers.chat_completions import usage_of

    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    assert "cached_tokens" not in usage_of(usage)


def test_a_cached_round_costs_less_than_an_uncached_one():
    from maajun.providers.chat_completions import usage_of
    from maajun.providers.pricing import extract_usage

    plain = SimpleNamespace(
        prompt_tokens=100_000, completion_tokens=100, total_tokens=100_100
    )
    cached = SimpleNamespace(
        prompt_tokens=100_000,
        completion_tokens=100,
        total_tokens=100_100,
        prompt_cache_hit_tokens=99_000,
    )
    _, _, full = extract_usage(usage_of(plain), DEFAULT_MODEL)
    _, _, discounted = extract_usage(usage_of(cached), DEFAULT_MODEL)
    assert discounted < full
