import asyncio
import inspect
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
        self.stream_usage = True

    def prepared_tools(self, tools: list[ToolDefinition] | None) -> list[dict[str, Any]] | None:
        # Deliberately uncached: memoizing on id(tools) never hit, and
        # CPython reuses freed addresses, so it could serve stale tools.
        if not tools:
            return None
        return self.prepare_tools(tools)

    async def initialize(self) -> None:
        if not self.api_key:
            raise ProviderError("API key is required. Run `maajun setup` to set one.")

        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    async def aclose(self) -> None:
        """Close the underlying HTTP client so its connection pool is freed."""
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def retryable(self, coro_func, *args, **kwargs):
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
                # Nothing follows the last attempt; sleeping after it only
                # delays an error the caller was always going to get.
                if not transient or attempt == MAX_RETRIES - 1:
                    break
                delay = min(MAX_DELAY, BASE_DELAY * (2 ** attempt) * (1 + random.random()))
                log.warning(
                    "API error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, MAX_RETRIES, e, delay,
                )
                await asyncio.sleep(delay)
        # Outside the except block, so the chain needs an explicit `from`.
        raise wrap_error(last_exc) from last_exc

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

        tool_defs = self.prepared_tools(tools)

        response = await self.retryable(
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
        """Stream text deltas; emit tool_calls and usage once at stream end."""
        if not self.client:
            await self.initialize()

        tool_defs = self.prepared_tools(tools)
        tool_calls = ToolCallAccumulator()
        usage: dict[str, int] | None = None

        stream = None
        try:
            stream = await self.open_stream(
                model=self.model,
                messages=messages,
                tools=tool_defs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            )

            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = usage_of(chunk.usage)
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
            raise wrap_error(e) from e
        finally:
            # An abandoned stream holds its connection until collected, and
            # a watch run opens one per tool round.
            if stream is not None:
                await close_quietly(stream)

        if tool_calls:
            yield "tool_calls", tool_calls.result()
        if usage:
            yield "usage", usage

    async def open_stream(self, **kwargs):
        """Start a stream, asking for token usage where the endpoint allows it.

        Without stream_options a streamed turn reports no usage at all, so it
        would cost real money and show as free. A gateway that doesn't
        implement the option rejects the request naming it; that one is
        retried plainly and the option is not offered again.
        """
        if self.stream_usage:
            try:
                return await self.retryable(
                    self.client.chat.completions.create,
                    stream_options={"include_usage": True},
                    **kwargs,
                )
            except ProviderError as e:
                if "stream_options" not in str(e):
                    raise
                log.info("endpoint rejected stream_options; usage will not be reported")
                self.stream_usage = False
        return await self.retryable(self.client.chat.completions.create, **kwargs)

    async def validate_credentials(self) -> bool:
        """Validate the credentials with a minimal request.

        Sends self.model, not default_model: with ai.model set to something
        the account cannot reach, validating the default reported a working
        key and then every real call failed on an inaccessible model. The
        check should exercise what the daemon will actually send.
        """
        try:
            if not self.client:
                await self.initialize()

            response = await self.retryable(
                self.client.chat.completions.create,
                model=self.model,
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
            # Left as raw JSON: the agent parses it, so malformed arguments
            # become a tool error rather than a crash here.
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

        usage = usage_of(response.usage) if response.usage else None

        return CompletionResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            raw_response=response,
            thinking=getattr(message, "reasoning_content", None),
            model=self.model,
        )


async def close_quietly(stream: Any) -> None:
    """Release a stream, tolerating one that is finished, sync, or has none."""
    closer = getattr(stream, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.debug("could not close the response stream", exc_info=True)


def usage_of(usage: Any) -> dict[str, int]:
    counts = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    cached = cached_tokens_of(usage)
    if cached is not None:
        counts["cached_tokens"] = cached
    return counts


def cached_tokens_of(usage: Any) -> int | None:
    """Prompt tokens served from the provider's prefix cache, if it says.

    A thirtieth of the price on DeepSeek, and the tool loop resends the same
    prefix every round. DeepSeek reports prompt_cache_hit_tokens, OpenAI
    nests the count under prompt_tokens_details. Absent is charged in full.
    """
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if isinstance(hit, int):
        return hit
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return cached if isinstance(cached, int) else None


class ToolCallAccumulator:
    """Reassembles tool calls from stream deltas.

    The API sends each tool call fragmented across chunks: id and name arrive
    once, arguments arrive as string fragments to be concatenated, and the
    call's position is identified by index.
    """

    def __init__(self) -> None:
        self.calls: dict[int, dict[str, Any]] = {}

    def __bool__(self) -> bool:
        return bool(self.calls)

    def add(self, deltas: list[Any]) -> None:
        for delta in deltas:
            call = self.calls.setdefault(delta.index, {
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
        return [self.calls[i] for i in sorted(self.calls)]


def wrap_error(e: APIError) -> ProviderError:
    if isinstance(e, AuthenticationError):
        return ProviderError(
            "API key is invalid. Run `maajun setup` to update it."
        )
    if isinstance(e, RateLimitError):
        return ProviderError("Rate limit reached. Wait a moment and try again.")
    if isinstance(e, APIConnectionError):
        return ProviderError(f"Could not reach provider: {e}")
    return ProviderError(f"Provider API error: {e}")

