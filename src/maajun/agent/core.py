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

# Per-request cap; the full history is kept locally for /history.
MAX_HISTORY_MESSAGES = 40

# One turn's tool loop adds messages that are not history yet, so
# MAX_HISTORY_MESSAGES alone does not bound a request. ~40k tokens.
MAX_REQUEST_CHARS = 160_000

# Floor, so one huge tool result cannot erase the question it answered.
MIN_REQUEST_MESSAGES = 4

TOOL_RESULT_PREVIEW = 200

# True approves, False denies, a string denies with a reason for the model.
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool | str]]

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


def message_size(message: dict[str, Any]) -> int:
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
    total = sum(message_size(message) for message in messages)
    while total > MAX_REQUEST_CHARS and len(messages) > MIN_REQUEST_MESSAGES:
        total -= message_size(messages.pop(1))
        while len(messages) > MIN_REQUEST_MESSAGES and messages[1].get("role") == "tool":
            total -= message_size(messages.pop(1))
    # The floor can stop having dropped an assistant message but not its tool
    # results. The API rejects an orphan, so the floor gives way instead.
    while len(messages) > 1 and messages[1].get("role") == "tool":
        total -= message_size(messages.pop(1))
    if total > MAX_REQUEST_CHARS:
        log.warning(
            "request is %d chars after trimming to %d messages — the provider "
            "may reject it as too long",
            total, len(messages),
        )


def is_tool_context(message: dict[str, Any]) -> bool:
    """Whether a message is a tool call or its result rather than plain text."""
    return message.get("role") == "tool" or bool(message.get("tool_calls"))


def drop_leading_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A tool result whose call has been trimmed away is rejected by the API."""
    start = 0
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    return messages[start:]


def accumulate_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
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
        self.history: list[dict[str, Any]] = []
        self.registry = tools or default_registry()
        self.approve = approve
        self.usage: dict[str, int] = {}
        # The daemon and chat want different instructions, same tool loop.
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

    @property
    def model(self) -> str | None:
        return getattr(self.provider, "model", None)

    def clear_history(self) -> None:
        self.history.clear()

    def take_usage(self) -> dict[str, int]:
        """The tokens spent since the last call, including a turn that failed.

        Every tool round is a billed request. A turn that dies on round thirty
        still spent what the first twenty-nine cost, so the caller reads this
        from a `finally` rather than from the response it never got.
        """
        usage, self.usage = self.usage, {}
        return usage

    async def aclose(self) -> None:
        """Release the provider's HTTP client."""
        await self.provider.aclose()

    async def chat(self, message: str) -> CompletionResponse:
        self.history.append({"role": "user", "content": message})
        self.trim_history()

        messages = self.request_messages()
        produced: list[dict[str, Any]] = []

        def emit(entry: dict[str, Any]) -> None:
            messages.append(entry)
            produced.append(entry)

        tools = self.registry.definitions()
        thinking_parts: list[str] = []
        last_content = ""
        # Every round is billed, and the spend cap reads these numbers.
        self.usage = {}

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                trim_request_messages(messages)
                response = await self.complete(messages, tools)
                accumulate_usage(self.usage, response.usage)

                if response.thinking:
                    thinking_parts.append(response.thinking)
                if response.content:
                    last_content = response.content

                if not response.tool_calls:
                    emit({"role": "assistant", "content": response.content})
                    self.commit_turn(produced)
                    return CompletionResponse(
                        content=response.content,
                        thinking="".join(thinking_parts) or None,
                        usage=self.usage or None,
                        model=response.model,
                    )

                async for _ in self.run_tools(emit, response):
                    pass

            emit({"role": "assistant", "content": last_content})
            self.commit_turn(produced)
            return CompletionResponse(
                content=last_content,
                thinking="".join(thinking_parts) or None,
                usage=self.usage or None,
                model=response.model,
            )

        except Exception:
            self.rollback_user_message()
            raise

    async def chat_stream(self, message: str) -> AsyncIterator[StreamChunk]:
        """Yield ("thinking" | "content" | "running" | "tool", text) chunks.

        Tool calls are handled transparently: the provider emits them as a
        single event once a round's stream ends, each is announced as a
        "running" chunk before it starts and yielded again as a "tool" chunk
        with its result preview, and the next round starts streaming.
        """
        self.history.append({"role": "user", "content": message})
        self.trim_history()

        messages = self.request_messages()
        produced: list[dict[str, Any]] = []

        def emit(entry: dict[str, Any]) -> None:
            messages.append(entry)
            produced.append(entry)

        tools = self.registry.definitions()
        content_parts: list[str] = []
        self.usage = {}

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
                    elif kind == "usage":
                        accumulate_usage(self.usage, data)
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
                for call in tool_calls:
                    yield "running", call["function"]["name"]
                async for name, result in self.run_tools(emit, response):
                    preview = result[:TOOL_RESULT_PREVIEW]
                    if len(result) > TOOL_RESULT_PREVIEW:
                        preview += "..."
                    yield "tool", f"🔧 {name} → {preview}"

            emit({"role": "assistant", "content": "".join(content_parts)})
            self.commit_turn(produced)

        except Exception:
            self.rollback_user_message()
            raise

    async def complete(self, messages: list[dict], tools: list) -> CompletionResponse:
        return await self.provider.chat_completion(
            messages=messages,
            tools=tools,
            temperature=self.config.ai.temperature,
            max_tokens=self.config.ai.max_tokens,
        )

    async def run_tools(
        self, emit: Callable[[dict[str, Any]], None], response: CompletionResponse
    ) -> AsyncIterator[tuple[str, str]]:
        """Execute the response's tool calls, emitting the assistant message
        and each tool result. Yields (tool_name, result).

        When a round has several calls that all need no approval (read-only
        tools like read_file/grep/glob), they run concurrently. If any call
        is permission-gated they run sequentially, so approval prompts stay
        ordered and interactive.
        """
        emit({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": self.format_tool_calls(response.tool_calls),
        })

        prepared = [
            (tc, tc["function"]["name"], self.parse_args(tc["function"]))
            for tc in response.tool_calls
        ]
        concurrent = len(prepared) > 1 and not any(
            self.registry.requires_permission(name) for _, name, _ in prepared
        )
        if concurrent:
            results = await asyncio.gather(
                *(self.execute_tool(name, args) for _, name, args in prepared)
            )
        else:
            results = [await self.execute_tool(name, args) for _, name, args in prepared]

        for (tc, name, _), result in zip(prepared, results, strict=True):
            emit({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            yield name, result

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        log.info("tool_call name=%s args=%s", name, args)
        verdict = await self.permitted(name, args)
        if verdict is True:
            result = await self.registry.execute(name, args)
            log.info("tool_result name=%s len=%d", name, len(result))
        else:
            result = PERMISSION_DENIED
            if isinstance(verdict, str):
                result += f" They said: {verdict}"
            log.info("tool_denied name=%s", name)
        return result

    async def permitted(self, name: str, args: dict[str, Any]) -> bool | str:
        if not self.registry.requires_permission(name):
            return True
        if self.approve is None:
            return False
        return await self.approve(name, args)

    def request_messages(self) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": self.system_prompt},
            *drop_leading_tool_results(self.history[-MAX_HISTORY_MESSAGES:]),
        ]

    def commit_turn(self, produced: list[dict[str, Any]]) -> None:
        """Fold a finished turn into the history the next one starts from.

        The turn's tool calls and their results are kept, so a follow-up
        question doesn't re-read a file the previous answer already read.
        Only the newest turn keeps them: older rounds collapse back to the
        conversation, which is what the user is still talking about.
        """
        self.history = [
            entry for entry in self.history if not is_tool_context(entry)
        ]
        self.history.extend(produced)
        self.trim_history()

    def trim_history(self) -> None:
        """Drop the oldest entries once history exceeds the cap."""
        if len(self.history) > MAX_HISTORY_MESSAGES:
            del self.history[: len(self.history) - MAX_HISTORY_MESSAGES]
        self.history = drop_leading_tool_results(self.history)

    def rollback_user_message(self) -> None:
        if self.history and self.history[-1]["role"] == "user":
            self.history.pop()

    @staticmethod
    def format_tool_calls(tool_calls: list[dict]) -> list[dict]:
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
    def parse_args(fn: dict) -> dict:
        try:
            args = (
                json.loads(fn["arguments"])
                if isinstance(fn["arguments"], str)
                else fn["arguments"]
            )
        except json.JSONDecodeError:
            return {}
        return args if isinstance(args, dict) else {}
