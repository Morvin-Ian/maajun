from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

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

# Ceiling on the characters in one request. MAX_HISTORY_MESSAGES bounds the
# stored conversation, but a single turn's tool loop appends an assistant
# message and a tool result per call for up to MAX_TOOL_ROUNDS rounds — none
# of which is history yet. Left unbounded, a grep-heavy analysis walks past
# the context window and fails the incident outright.
#
# ~4 chars per token puts this near 40k tokens, comfortably inside the
# smallest context maajun targets while leaving room for max_tokens of output.
MAX_REQUEST_CHARS = 160_000

# Never trim below the system prompt plus this many trailing messages, so a
# single enormous tool result cannot erase the question it was answering.
MIN_REQUEST_MESSAGES = 4

TOOL_RESULT_PREVIEW = 200

# Called with (tool_name, arguments) before a permission-gated tool runs;
# returns whether the call is approved.
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]

PERMISSION_DENIED = (
    "Error: the user denied permission for this tool call. "
    "Do not retry it — adjust your approach or ask the user what to do."
)

SYSTEM_PROMPT = """\
You are Maajun, an expert AI coding assistant with access to tools.

You can read, search, and edit files, and inspect git repositories.  Use
tools whenever they help you give a better answer.

You have no shell access: there is no tool that runs commands, so you cannot
run tests, install anything, or execute the code you are reading.  Never
claim to have done so — reason from the source you can read.

When editing files:
- Always read the file first with read_file to see its current contents.
- For edit_file, the old_string must match exactly once — include enough
  surrounding context to make it unique.
- If old_string is not found, re-read the file and try again.

edit_file and write_file are permission-gated and may be refused; every other
tool is always available.  If a call is denied, do not retry it — say what you
wanted to change and continue with the rest of your answer.  You may be
running unattended, so never end by waiting for a reply.

Be concise, accurate, and helpful.  Use markdown when it improves readability.
If you're unsure about something, say so rather than guessing."""


def _message_size(message: dict[str, Any]) -> int:
    """Rough character cost of one message, including its tool calls."""
    size = len(message.get("content") or "")
    for call in message.get("tool_calls") or ():
        function = call.get("function", {})
        size += len(function.get("name", "")) + len(function.get("arguments") or "")
    return size


def trim_request_messages(messages: list[dict[str, Any]]) -> None:
    """Drop the oldest rounds in place until the request fits the budget.

    messages[0] is the system prompt and is never dropped. Everything else is
    removed oldest-first, except that a "tool" message is never left at the
    front: it belongs to the assistant message that requested it, and the API
    rejects a tool result with no matching tool call ahead of it.
    """
    total = sum(_message_size(message) for message in messages)
    while total > MAX_REQUEST_CHARS and len(messages) > MIN_REQUEST_MESSAGES:
        total -= _message_size(messages.pop(1))
        while len(messages) > MIN_REQUEST_MESSAGES and messages[1].get("role") == "tool":
            total -= _message_size(messages.pop(1))
    if total > MAX_REQUEST_CHARS:
        log.warning(
            "request is %d chars after trimming to %d messages — the provider "
            "may reject it as too long",
            total, len(messages),
        )


def _accumulate_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
    """Add one response's token counts into a running total.

    Sums whatever keys the provider reported rather than a fixed set, so a
    provider that adds e.g. cached-token counts is carried through instead of
    being silently dropped.
    """
    if not usage:
        return
    for key, value in usage.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


class Agent:
    def __init__(
        self,
        config: Config,
        *,
        tools: ToolRegistry | None = None,
        approve: PermissionCallback | None = None,
        system_prompt: str | None = None,
    ):
        self.config = config
        self.history: list[dict[str, str]] = []
        self.registry = tools or default_registry()
        self.approve = approve
        # The daemon's incident analysis and an interactive chat want to be
        # told different things; the tool loop underneath is identical.
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.provider = ProviderFactory.create_provider(
            ProviderType(config.ai.provider),
            {
                "api_key": config.ai.api_key,
                "model": config.ai.model,
                "base_url": config.ai.base_url,
                "thinking_mode": config.ai.thinking_mode,
            },
        )

    def clear_history(self) -> None:
        self.history.clear()

    async def aclose(self) -> None:
        """Release the provider's HTTP client."""
        await self.provider.aclose()

    async def chat(self, message: str) -> CompletionResponse:
        self.history.append({"role": "user", "content": message})
        self._trim_history()

        messages = self._request_messages()
        tools = self.registry.definitions()
        thinking_parts: list[str] = []
        last_content = ""
        # Every round is a billed request, and each one resends the whole
        # conversation — so reporting only the final round's usage would
        # under-count a tool-heavy analysis several times over, and the
        # daemon's spend cap reads these numbers.
        total_usage: dict[str, int] = {}

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                trim_request_messages(messages)
                response = await self._complete(messages, tools)
                _accumulate_usage(total_usage, response.usage)

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
                        usage=total_usage or None,
                        model=response.model,
                    )

                async for _name, _result in self._run_tools(messages, response):
                    pass

            self.history.append({"role": "assistant", "content": last_content})
            return CompletionResponse(
                content=last_content,
                thinking="".join(thinking_parts) or None,
                usage=total_usage or None,
                model=response.model,
            )

        except Exception:
            self._rollback_user_message()
            raise

    async def chat_stream(self, message: str) -> AsyncIterator[StreamChunk]:
        """Yield ("thinking" | "content" | "tool", text) chunks as they arrive.

        Tool calls are handled transparently: the provider emits them as a
        single event once a round's stream ends, the tools run and each result
        preview is yielded as a "tool" chunk, and the next round starts
        streaming.
        """
        self.history.append({"role": "user", "content": message})
        self._trim_history()

        messages = self._request_messages()
        tools = self.registry.definitions()
        content_parts: list[str] = []

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                round_content: list[str] = []
                tool_calls: list[dict] = []

                trim_request_messages(messages)
                async for kind, data in self.provider.stream_completion(
                    messages=messages,
                    tools=tools,
                    temperature=self.config.ai.temperature,
                    max_tokens=self.config.ai.max_tokens,
                ):
                    if kind == "tool_calls":
                        tool_calls = data
                    else:
                        if kind == "content":
                            round_content.append(data)
                        yield kind, data

                content_parts.extend(round_content)

                if not tool_calls:
                    break

                response = CompletionResponse(
                    content="".join(round_content),
                    tool_calls=tool_calls,
                )
                async for name, result in self._run_tools(messages, response):
                    preview = result[:TOOL_RESULT_PREVIEW]
                    if len(result) > TOOL_RESULT_PREVIEW:
                        preview += "..."
                    yield "tool", f"🔧 {name} → {preview}"

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
        and each tool result to messages. Yields (tool_name, result).

        When a round has several calls that all need no approval (read-only
        tools like read_file/grep/glob), they run concurrently. If any call
        is permission-gated they run sequentially, so approval prompts stay
        ordered and interactive.
        """
        messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": self._format_tool_calls(response.tool_calls),
        })

        prepared = [
            (tc, tc["function"]["name"], self._parse_args(tc["function"]))
            for tc in response.tool_calls
        ]
        concurrent = len(prepared) > 1 and not any(
            self.registry.requires_permission(name) for _, name, _ in prepared
        )
        if concurrent:
            results = await asyncio.gather(
                *(self._execute_tool(name, args) for _, name, args in prepared)
            )
        else:
            results = [await self._execute_tool(name, args) for _, name, args in prepared]

        for (tc, name, _args), result in zip(prepared, results, strict=True):
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            yield name, result

    async def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        log.info("tool_call name=%s args=%s", name, args)
        if await self._permitted(name, args):
            result = await self.registry.execute(name, args)
            log.info("tool_result name=%s len=%d", name, len(result))
        else:
            result = PERMISSION_DENIED
            log.info("tool_denied name=%s", name)
        return result

    async def _permitted(self, name: str, args: dict[str, Any]) -> bool:
        if not self.registry.requires_permission(name):
            return True
        if self.approve is None:
            return False
        return await self.approve(name, args)

    def _request_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system_prompt},
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
