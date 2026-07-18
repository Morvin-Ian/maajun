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
