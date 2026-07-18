from collections.abc import AsyncIterator

from maajun.config import Config
from maajun.providers.base import CompletionResponse, ProviderType, StreamChunk
from maajun.providers.factory import ProviderFactory

# Cap the messages sent per request so long sessions don't blow the context
# window. Full history is kept locally for /history.
MAX_HISTORY_MESSAGES = 40

SYSTEM_PROMPT = """You are Maajun, an expert AI coding assistant. You help developers with:

- Debugging errors and understanding stack traces
- Writing, reviewing, and explaining code
- Refactoring and improving code quality
- Answering technical questions
- Planning software architecture

Be concise, accurate, and helpful. Use markdown formatting when it improves readability.
If you're unsure about something, say so rather than guessing."""


class Agent:
    def __init__(self, config: Config):
        self.config = config
        self.history: list[dict[str, str]] = []
        self.provider = ProviderFactory.create_provider(
            ProviderType(config.ai.provider),
            {
                "api_key": config.ai.api_key,
                "model": config.ai.model,
                "thinking_mode": config.ai.thinking_mode,
            },
        )

    def _request_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.history[-MAX_HISTORY_MESSAGES:],
        ]

    async def chat(self, message: str) -> CompletionResponse:
        """Send a chat message and return the full response"""
        self.history.append({"role": "user", "content": message})
        try:
            response = await self.provider.chat_completion(
                messages=self._request_messages(),
                temperature=self.config.ai.temperature,
                max_tokens=self.config.ai.max_tokens,
            )
        except Exception:
            self.history.pop()
            raise

        self.history.append({"role": "assistant", "content": response.content})
        return response

    async def chat_stream(self, message: str) -> AsyncIterator[StreamChunk]:
        """Send a chat message, yielding ("thinking" | "content", text) chunks.

        History is updated once the stream finishes; on error the user
        message is rolled back so the conversation stays consistent.
        """
        self.history.append({"role": "user", "content": message})
        content_parts: list[str] = []
        try:
            async for kind, text in self.provider.stream_completion(
                messages=self._request_messages(),
                temperature=self.config.ai.temperature,
                max_tokens=self.config.ai.max_tokens,
            ):
                if kind == "content":
                    content_parts.append(text)
                yield kind, text
        except Exception:
            self.history.pop()
            raise

        self.history.append({"role": "assistant", "content": "".join(content_parts)})
