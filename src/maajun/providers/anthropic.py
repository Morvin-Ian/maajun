from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from anthropic import (
    APIConnectionError,
    APIError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from .base import (
    AIProvider,
    CompletionResponse,
    ModelInfo,
    ProviderError,
    ProviderType,
    StreamChunk,
    ToolDefinition,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 2

# Models that think adaptively and reject temperature/top_p outright.
ADAPTIVE_MODELS = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
)


class AnthropicProvider(AIProvider):
    """Claude on the Messages API.

    Not a ChatCompletionsProvider: the system prompt is hoisted out of the
    messages, tool results are content blocks, and cache breakpoints are
    explicit. The agent still sees OpenAI-shaped tool calls either way.
    """

    name = ProviderType.ANTHROPIC.value
    default_model = "claude-haiku-4-5"
    thinking_model = "claude-opus-5"
    models = (
        ModelInfo("claude-haiku-4-5", "The fastest and cheapest Claude."),
        ModelInfo(
            "claude-sonnet-5",
            "Mid tier: more capable than Haiku, well under Opus in price.",
        ),
        ModelInfo("claude-opus-5", "The most capable, and the dearest."),
    )

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client: AsyncAnthropic | None = None

    async def initialize(self) -> None:
        if not self.api_key:
            raise ProviderError("API key is required. Run `maajun setup` to set one.")
        self.client = AsyncAnthropic(
            api_key=self.api_key, base_url=self.base_url, max_retries=MAX_RETRIES
        )

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    def prepare_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    def request(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        system, turns = split_system(messages)
        if not turns:
            raise ProviderError("Nothing to send: the request had no messages.")

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": turns,
            # Anthropic caches only where told to. Without this every tool
            # round re-reads the whole prompt at full price.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            params["system"] = system
        prepared = self.prepared_tools(tools)
        if prepared:
            params["tools"] = prepared
        if self.model.startswith(ADAPTIVE_MODELS):
            # Sampling is rejected on these, and thinking is why they cost more.
            params["thinking"] = {"type": "adaptive", "display": "summarized"}
        else:
            params["temperature"] = temperature
        return params

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> CompletionResponse:
        if not self.client:
            await self.initialize()

        params = self.request(messages, tools, temperature, max_tokens)
        try:
            message = await self.client.messages.create(**params, **kwargs)
        except APIError as e:
            raise wrap_error(e) from e
        return self.parse_message(message)

    async def stream_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        if not self.client:
            await self.initialize()

        params = self.request(messages, tools, temperature, max_tokens)
        try:
            async with self.client.messages.stream(**params, **kwargs) as stream:
                async for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield "content", delta.text
                    elif delta.type == "thinking_delta":
                        yield "thinking", delta.thinking
                final = await stream.get_final_message()
        except APIError as e:
            raise wrap_error(e) from e

        response = self.parse_message(final)
        if response.tool_calls:
            yield "tool_calls", response.tool_calls
        if response.usage:
            yield "usage", response.usage

    async def validate_credentials(self) -> bool:
        try:
            if not self.client:
                await self.initialize()
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "Hello"}],
            )
            return bool(message.content is not None)
        except (APIError, ProviderError):
            return False

    def parse_message(self, message: Any) -> CompletionResponse:
        if message.stop_reason == "refusal":
            raise ProviderError(refusal_reason(message))

        text: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in message.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "thinking":
                thinking.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })

        return CompletionResponse(
            content="".join(text),
            tool_calls=tool_calls or None,
            finish_reason=message.stop_reason or "stop",
            usage=usage_of(message.usage),
            raw_response=message,
            thinking="".join(thinking) or None,
            model=self.model,
        )


def split_system(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Hoist the system prompt out and translate the rest into Messages API turns."""
    system: list[str] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            if message.get("content"):
                system.append(message["content"])
        elif role == "tool":
            add_tool_result(turns, message)
        elif role == "assistant":
            add_assistant(turns, message)
        else:
            turns.append({"role": "user", "content": message.get("content") or ""})

    # A turn list that opens on the assistant is rejected, and trimming the
    # oldest rounds can leave one there.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return "\n\n".join(system), turns


def add_tool_result(turns: list[dict[str, Any]], message: dict[str, Any]) -> None:
    """Append one tool result, merging into the batch it belongs to.

    Every result for one assistant turn has to arrive in a single user
    message; the agent emits them one apiece.
    """
    block = {
        "type": "tool_result",
        "tool_use_id": message.get("tool_call_id", ""),
        "content": message.get("content") or "",
    }
    if turns and is_tool_result_turn(turns[-1]):
        turns[-1]["content"].append(block)
    else:
        turns.append({"role": "user", "content": [block]})


def is_tool_result_turn(turn: dict[str, Any]) -> bool:
    content = turn.get("content")
    return (
        turn.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and content[0].get("type") == "tool_result"
    )


def add_assistant(turns: list[dict[str, Any]], message: dict[str, Any]) -> None:
    content: list[dict[str, Any]] = []
    text = (message.get("content") or "").strip()
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or ():
        function = call.get("function", {})
        content.append({
            "type": "tool_use",
            "id": call.get("id", ""),
            "name": function.get("name", ""),
            "input": parse_arguments(function.get("arguments")),
        })
    # An empty assistant turn is rejected; it carries nothing anyway.
    if content:
        turns.append({"role": "assistant", "content": content})


def parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def usage_of(usage: Any) -> dict[str, int]:
    """Token counts in the shape pricing.extract_usage reads.

    Anthropic's input_tokens excludes what the cache served, so the three are
    summed back into one prompt total with the split kept alongside.
    """
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    fresh = usage.input_tokens or 0
    output = usage.output_tokens or 0
    return {
        "prompt_tokens": fresh + read + written,
        "completion_tokens": output,
        "total_tokens": fresh + read + written + output,
        "cached_tokens": read,
        "cache_write_tokens": written,
    }


def refusal_reason(message: Any) -> str:
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None)
    suffix = f" ({category})" if category else ""
    return (
        f"Claude declined this request{suffix}. Rephrase it, or run the "
        "analysis on another provider."
    )


def wrap_error(e: APIError) -> ProviderError:
    if isinstance(e, AuthenticationError):
        return ProviderError("API key is invalid. Run `maajun setup` to update it.")
    if isinstance(e, RateLimitError):
        return ProviderError("Rate limit reached. Wait a moment and try again.")
    if isinstance(e, APIConnectionError):
        return ProviderError(f"Could not reach provider: {e}")
    return ProviderError(f"Provider API error: {e}")
