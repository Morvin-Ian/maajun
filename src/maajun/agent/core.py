from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from maajun.agent.tools import ToolRegistry, default_registry
from maajun.config import Config
from maajun.providers.base import (
    CompletionResponse,
    ProviderType,
    StreamChunk,
)
from maajun.providers.factory import ProviderFactory

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 50

# Cap the messages sent per request so long sessions don't blow the context
# window. Full history is kept locally for /history.
MAX_HISTORY_MESSAGES = 40

TOOL_RESULT_PREVIEW = 200

SYSTEM_PROMPT = """\
You are Maajun, an expert AI coding assistant with access to tools.

You can read, search, and edit files, run shell commands, and inspect git
repositories.  Use tools whenever they help you give a better answer.

When editing files:
- Always read the file first with read_file to see its current contents.
- For edit_file, the old_string must match exactly once — include enough
  surrounding context to make it unique.
- If old_string is not found, re-read the file and try again.

Be concise, accurate, and helpful.  Use markdown when it improves readability.
If you're unsure about something, say so rather than guessing."""


class Agent:
    def __init__(
        self,
        config: Config,
        *,
        tools: ToolRegistry | None = None,
    ):
        self.config = config
        self.history: list[dict[str, str]] = []
        self.registry = tools or default_registry()
        self.provider = ProviderFactory.create_provider(
            ProviderType(config.ai.provider),
            {
                "api_key": config.ai.api_key,
                "model": config.ai.model,
                "thinking_mode": config.ai.thinking_mode,
            },
        )

    def clear_history(self) -> None:
        self.history.clear()

    async def chat(self, message: str) -> CompletionResponse:
        self.history.append({"role": "user", "content": message})
        self._trim_history()

        messages = self._request_messages()
        tools = self.registry.definitions()
        thinking_parts: list[str] = []
        last_content = ""

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await self._complete(messages, tools)

                if response.thinking:
                    thinking_parts.append(response.thinking)
                if response.content:
                    last_content = response.content

                if not response.tool_calls:
                    self.history.append({
                        "role": "assistant",
                        "content": response.content,
                    })
                    return CompletionResponse(
                        content=response.content,
                        thinking="".join(thinking_parts) or None,
                        usage=response.usage,
                    )

                async for _name, _result in self._run_tools(messages, response):
                    pass

            self.history.append({"role": "assistant", "content": last_content})
            return CompletionResponse(
                content=last_content,
                thinking="".join(thinking_parts) or None,
            )

        except Exception:
            self._rollback_user_message()
            raise

    async def chat_stream(self, message: str) -> AsyncIterator[StreamChunk]:
        """Yield ("thinking" | "content", text) chunks.

        Tool-call rounds use non-streaming requests because DeepSeek does not
        reliably stream tool_calls; each round's output is yielded as soon as
        it completes.
        """
        self.history.append({"role": "user", "content": message})
        self._trim_history()

        messages = self._request_messages()
        tools = self.registry.definitions()
        content_parts: list[str] = []

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await self._complete(messages, tools)

                if response.thinking:
                    yield "thinking", response.thinking
                if response.content:
                    content_parts.append(response.content)
                    yield "content", response.content

                if not response.tool_calls:
                    break

                async for name, result in self._run_tools(messages, response):
                    preview = result[:TOOL_RESULT_PREVIEW]
                    if len(result) > TOOL_RESULT_PREVIEW:
                        preview += "..."
                    yield "thinking", f"\n\n🔧 {name} → {preview}\n\n"

            self.history.append({
                "role": "assistant",
                "content": "".join(content_parts),
            })

        except Exception:
            self._rollback_user_message()
            raise

    async def _complete(self, messages: list[dict], tools: list) -> CompletionResponse:
        return await self.provider.chat_completion(
            messages=messages,
            tools=tools,
            temperature=self.config.ai.temperature,
            max_tokens=self.config.ai.max_tokens,
        )

    async def _run_tools(
        self, messages: list[dict], response: CompletionResponse
    ) -> AsyncIterator[tuple[str, str]]:
        """Execute the response's tool calls, appending the assistant message
        and each tool result to messages. Yields (tool_name, result)."""
        messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": self._format_tool_calls(response.tool_calls),
        })

        for tc in response.tool_calls:
            fn = tc["function"]
            name = fn["name"]
            args = self._parse_args(fn)

            log.info("tool_call name=%s args=%s", name, args)
            result = await self.registry.execute(name, args)
            log.info("tool_result name=%s len=%d", name, len(result))

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            yield name, result

    def _request_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.history[-MAX_HISTORY_MESSAGES:],
        ]

    def _trim_history(self) -> None:
        """Drop oldest user/assistant pairs if history exceeds the cap."""
        while len(self.history) > MAX_HISTORY_MESSAGES:
            self.history.pop(0)
            if self.history and self.history[0]["role"] == "assistant":
                self.history.pop(0)

    def _rollback_user_message(self) -> None:
        if self.history and self.history[-1]["role"] == "user":
            self.history.pop()

    @staticmethod
    def _format_tool_calls(tool_calls: list[dict]) -> list[dict]:
        """Normalize tool_call dicts for the messages API (arguments must be
        a JSON string on the wire)."""
        return [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": (
                        tc["function"]["arguments"]
                        if isinstance(tc["function"]["arguments"], str)
                        else json.dumps(tc["function"]["arguments"])
                    ),
                },
            }
            for tc in tool_calls
        ]

    @staticmethod
    def _parse_args(fn: dict) -> dict:
        try:
            args = (
                json.loads(fn["arguments"])
                if isinstance(fn["arguments"], str)
                else fn["arguments"]
            )
        except json.JSONDecodeError:
            return {}
        return args if isinstance(args, dict) else {}
