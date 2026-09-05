from types import SimpleNamespace

import pytest

from maajun.providers.anthropic import (
    AnthropicProvider,
    parse_arguments,
    split_system,
    usage_of,
)
from maajun.providers.base import ProviderError, ToolDefinition


def usage(input_tokens=10, output_tokens=5, read=0, written=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=written,
    )


def message(*blocks, stop_reason="end_turn", **kwargs):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=kwargs.pop("usage", usage()),
        **kwargs,
    )


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(id="t1", name="read_file", input=None):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input or {})


def provider(**config):
    return AnthropicProvider({"api_key": "k", **config})


# ---------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------


def test_the_system_prompt_is_hoisted_out_of_the_messages():
    """Anthropic takes it as its own parameter, not as messages[0]."""
    system, turns = split_system([
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
    ])
    assert system == "be helpful"
    assert turns == [{"role": "user", "content": "hi"}]


def test_several_system_messages_are_joined():
    system, _ = split_system([
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"},
        {"role": "user", "content": "hi"},
    ])
    assert system == "one\n\ntwo"


def test_an_assistant_tool_call_becomes_a_tool_use_block():
    _, turns = split_system([
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
            }],
        },
    ])
    assert turns[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "on it"},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "read_file",
                "input": {"path": "a.py"},
            },
        ],
    }


def test_tool_results_for_one_turn_are_merged_into_a_single_user_message():
    """Anthropic rejects them split across messages, and the agent emits one
    message per result."""
    _, turns = split_system([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "grep", "arguments": "{}"}},
            {"id": "b", "function": {"name": "glob", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "found"},
        {"role": "tool", "tool_call_id": "b", "content": "nothing"},
    ])
    assert len(turns) == 3
    assert [block["tool_use_id"] for block in turns[2]["content"]] == ["a", "b"]


def test_a_second_round_of_tool_results_starts_a_new_message():
    _, turns = split_system([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "found"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "found again"},
    ])
    assert [turn["role"] for turn in turns] == [
        "user", "assistant", "user", "assistant", "user"
    ]


def test_an_empty_assistant_turn_is_dropped():
    """It carries nothing, and the API rejects a message with no content."""
    _, turns = split_system([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
    ])
    assert turns == [{"role": "user", "content": "hi"}]


def test_a_turn_list_never_opens_on_the_assistant():
    """Trimming the oldest rounds can leave one there, and it is rejected."""
    _, turns = split_system([
        {"role": "assistant", "content": "orphaned"},
        {"role": "user", "content": "hi"},
    ])
    assert turns[0]["role"] == "user"


def test_malformed_tool_arguments_become_an_empty_input():
    assert parse_arguments("not json") == {}
    assert parse_arguments(None) == {}
    assert parse_arguments('["a list"]') == {}
    assert parse_arguments('{"ok": 1}') == {"ok": 1}


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def test_the_request_asks_for_a_cache_breakpoint():
    """Anthropic caches only where it is told to; without this every tool
    round re-reads the whole prompt at full price."""
    params = provider().request(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        None, 0.3, 4096,
    )
    assert params["cache_control"] == {"type": "ephemeral"}


def test_tools_are_translated_to_the_messages_api_shape():
    tools = [ToolDefinition("grep", "search", {"type": "object", "properties": {}})]
    params = provider().request([{"role": "user", "content": "hi"}], tools, 0.3, 4096)
    assert params["tools"] == [{
        "name": "grep",
        "description": "search",
        "input_schema": {"type": "object", "properties": {}},
    }]


def test_the_thinking_model_thinks_and_sends_no_temperature():
    """Sampling is rejected outright on the adaptive-thinking models."""
    params = provider(thinking_mode=True).request(
        [{"role": "user", "content": "hi"}], None, 0.3, 4096
    )
    assert params["model"] == AnthropicProvider.thinking_model
    assert params["thinking"]["type"] == "adaptive"
    assert "temperature" not in params


def test_the_default_model_takes_a_temperature_and_does_not_think():
    params = provider().request([{"role": "user", "content": "hi"}], None, 0.3, 4096)
    assert params["model"] == AnthropicProvider.default_model
    assert params["temperature"] == 0.3
    assert "thinking" not in params


def test_a_request_with_nothing_to_send_is_refused_before_the_call():
    with pytest.raises(ProviderError, match="no messages"):
        provider().request([{"role": "system", "content": "sys"}], None, 0.3, 4096)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def test_tool_calls_come_back_in_the_shape_the_agent_expects():
    """The agent speaks OpenAI tool calls whichever provider answered."""
    response = provider().parse_message(
        message(text_block("looking"), tool_block(input={"path": "a.py"}))
    )
    assert response.content == "looking"
    assert response.tool_calls == [{
        "id": "t1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
    }]


def test_thinking_blocks_are_collected_separately():
    thinking = SimpleNamespace(type="thinking", thinking="hmm")
    response = provider().parse_message(message(thinking, text_block("answer")))
    assert response.thinking == "hmm"
    assert response.content == "answer"


def test_a_refusal_is_raised_rather_than_returned_as_an_empty_report():
    """Silently returning nothing would file an empty issue."""
    refused = message(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(ProviderError, match="cyber"):
        provider().parse_message(refused)


def test_usage_sums_cached_tokens_back_into_the_prompt_total():
    """Anthropic's input_tokens excludes what the cache served; the rest of
    maajun expects prompt_tokens to be the whole prompt."""
    counts = usage_of(usage(input_tokens=100, output_tokens=7, read=900, written=50))
    assert counts["prompt_tokens"] == 1050
    assert counts["cached_tokens"] == 900
    assert counts["cache_write_tokens"] == 50
    assert counts["completion_tokens"] == 7
    assert counts["total_tokens"] == 1057


def test_a_cached_turn_is_costed_below_an_uncached_one():
    from maajun.providers.pricing import extract_usage

    fresh = usage_of(usage(input_tokens=100_000, output_tokens=100))
    cached = usage_of(usage(input_tokens=1_000, output_tokens=100, read=99_000))
    _, _, full = extract_usage(fresh, AnthropicProvider.default_model)
    _, _, discounted = extract_usage(cached, AnthropicProvider.default_model)
    assert discounted < full / 5


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_without_a_key_says_how_to_fix_it():
    with pytest.raises(ProviderError, match="maajun setup"):
        await AnthropicProvider({}).initialize()


def test_the_configured_model_wins_over_both_defaults():
    assert provider(model="claude-sonnet-5", thinking_mode=True).model == "claude-sonnet-5"
