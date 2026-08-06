import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from .base import (
    AIProvider,
    CompletionResponse,
    ProviderError,
    StreamChunk,
    ToolDefinition,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0

NON_RETRYABLE = (AuthenticationError,)

class ChatCompletionsProvider(AIProvider): 
    name: str = ""
    base_url: str | None = None
    default_model: str = ""
    thinking_model: str = ""

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url") or self.base_url
        configured_model = config.get("model")
        if configured_model:
            self.model = configured_model
        elif config.get("thinking_mode") and self.thinking_model:
            self.model = self.thinking_model
        else:
            self.model = self.default_model
        self.client: AsyncOpenAI | None = None
        self._tool_cache: tuple[int, list[dict[str, Any]]] | None = None

    def _prepared_tools(self, tools: list[ToolDefinition] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        key = id(tools)
        if self._tool_cache is None or self._tool_cache[0] != key:
            self._tool_cache = (key, self.prepare_tools(tools))
        return self._tool_cache[1]

    async def initialize(self) -> None:
        if not self.api_key:
            raise ProviderError("API key is required. Run `maajun setup` to set one.")

        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    async def aclose(self) -> None:
        """Close the underlying HTTP client so its connection pool is freed."""
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def _retryable(self, coro_func, *args, **kwargs):
        """Call coro_func with retries on transient errors (429, 500, 502, 503, connection)."""
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                return await coro_func(*args, **kwargs)
            except NON_RETRYABLE:
                raise
            except (RateLimitError, APIConnectionError, APIError) as e:
                last_exc = e
                status = getattr(e, "status_code", None)
                transient = isinstance(e, APIConnectionError) or (
                    isinstance(e, APIError) and status in (429, 500, 502, 503)
                )
                if transient:
                    delay = min(MAX_DELAY, BASE_DELAY * (2 ** attempt) * (1 + random.random()))
                    log.warning(
                        "API error (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, MAX_RETRIES, e, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    break
        raise _wrap_error(last_exc)

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

        tool_defs = self._prepared_tools(tools)

        response = await self._retryable(
            self.client.chat.completions.create,
            model=self.model,
            messages=messages,
            tools=tool_defs,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

        return self.parse_response(response)

    async def stream_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """Stream text deltas; emit accumulated tool_calls once at stream end."""
        if not self.client:
            await self.initialize()

        tool_defs = self._prepared_tools(tools)
        tool_calls = _ToolCallAccumulator()

        try:
            stream = await self._retryable(
                self.client.chat.completions.create,
                model=self.model,
                messages=messages,
                tools=tool_defs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            )

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                thinking = getattr(delta, "reasoning_content", None)
                if thinking:
                    yield "thinking", thinking
                if delta.content:
                    yield "content", delta.content
                if delta.tool_calls:
                    tool_calls.add(delta.tool_calls)
        except ProviderError:
            raise
        except APIError as e:
            raise _wrap_error(e) from e

        if tool_calls:
            yield "tool_calls", tool_calls.result()

    async def validate_credentials(self) -> bool:
        """Validate the credentials with a minimal request."""
        try:
            if not self.client:
                await self.initialize()

            response = await self._retryable(
                self.client.chat.completions.create,
                model=self.default_model,
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=5,
            )
            return bool(response.choices)
        except (APIError, ProviderError):
            return False

    def get_provider_name(self) -> str:
        return self.name

    def clean_content(self, text: str) -> str:
        """Hook for stripping provider-specific markup from message content."""
        return text

    def parse_response(self, response: Any) -> CompletionResponse:
        choice = response.choices[0]
        message = choice.message

        tool_calls = None
        if message.tool_calls:
            # Keep arguments as the raw JSON string; the agent parses them
            # and malformed JSON becomes a tool error instead of a crash here.
            tool_calls = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]

        content = self.clean_content(message.content or "")

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return CompletionResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            raw_response=response,
            thinking=getattr(message, "reasoning_content", None),
            model=self.model,
        )


class _ToolCallAccumulator:
    """Reassembles tool calls from stream deltas.

    The API sends each tool call fragmented across chunks: id and name arrive
    once, arguments arrive as string fragments to be concatenated, and the
    call's position is identified by index.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def __bool__(self) -> bool:
        return bool(self._calls)

    def add(self, deltas: list[Any]) -> None:
        for delta in deltas:
            call = self._calls.setdefault(delta.index, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if delta.id:
                call["id"] = delta.id
            if delta.function:
                if delta.function.name:
                    call["function"]["name"] = delta.function.name
                if delta.function.arguments:
                    call["function"]["arguments"] += delta.function.arguments

    def result(self) -> list[dict[str, Any]]:
        return [self._calls[i] for i in sorted(self._calls)]


def _wrap_error(e: APIError) -> ProviderError:
    if isinstance(e, AuthenticationError):
        return ProviderError(
            "API key is invalid. Run `maajun setup` to update it."
        )
    if isinstance(e, RateLimitError):
        return ProviderError("Rate limit reached. Wait a moment and try again.")
    if isinstance(e, APIConnectionError):
        return ProviderError(f"Could not reach provider: {e}")
    return ProviderError(f"Provider API error: {e}")

