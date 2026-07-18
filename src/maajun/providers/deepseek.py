import json
import re
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
    ProviderType,
    StreamChunk,
    ToolDefinition,
)

_DSML_RE = re.compile(r"<\|+DSML\|+>.*?</\|+DSML\|+tool_calls>", re.DOTALL)
_DSML_OPEN_RE = re.compile(r"<\|+DSML\|+[^>]*>")

DEFAULT_MODEL = "deepseek-chat"
# DeepSeek exposes reasoning via a dedicated model, not a request flag.
THINKING_MODEL = "deepseek-reasoner"


class DeepSeekProvider(AIProvider):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url") or "https://api.deepseek.com"
        self.model = config.get("model") or DEFAULT_MODEL
        if config.get("thinking_mode"):
            self.model = THINKING_MODEL
        self.client: AsyncOpenAI | None = None

    async def initialize(self) -> None:
        if not self.api_key:
            raise ProviderError("DeepSeek API key is required. Run `maajun login` to set one.")

        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> CompletionResponse:
        """Send chat completion to DeepSeek"""
        if not self.client:
            await self.initialize()

        tool_defs = self.prepare_tools(tools) if tools else None

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tool_defs,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except APIError as e:
            raise _wrap_error(e) from e

        return self.parse_response(response)

    async def stream_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """Stream completion from DeepSeek as ("thinking" | "content", text) chunks"""
        if not self.client:
            await self.initialize()

        tool_defs = self.prepare_tools(tools) if tools else None

        try:
            stream = await self.client.chat.completions.create(
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
        except APIError as e:
            raise _wrap_error(e) from e

    async def validate_credentials(self) -> bool:
        """Validate DeepSeek credentials with a minimal request"""
        try:
            if not self.client:
                await self.initialize()

            response = await self.client.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=5,
            )
            return bool(response.choices)
        except (APIError, ProviderError):
            return False

    def get_provider_name(self) -> str:
        return ProviderType.DEEPSEEK.value

    def parse_response(self, response: Any) -> CompletionResponse:
        choice = response.choices[0]
        message = choice.message

        tool_calls = None
        if message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments),
                    },
                }
                for tc in message.tool_calls
            ]

        content = _strip_dsml(message.content or "")

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
        )


def _wrap_error(e: APIError) -> ProviderError:
    if isinstance(e, AuthenticationError):
        return ProviderError(
            "DeepSeek rejected the API key. Run `maajun login` to update it."
        )
    if isinstance(e, RateLimitError):
        return ProviderError("DeepSeek rate limit reached. Wait a moment and try again.")
    if isinstance(e, APIConnectionError):
        return ProviderError(f"Could not reach DeepSeek: {e}")
    return ProviderError(f"DeepSeek API error: {e}")


def _strip_dsml(text: str) -> str:
    text = _DSML_RE.sub("", text)
    text = _DSML_OPEN_RE.sub("", text)
    return text.strip()
