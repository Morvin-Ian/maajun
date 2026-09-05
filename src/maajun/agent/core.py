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
from maajun.providers.pricing import extract_usage

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 50

# Per-request cap; the full history is kept locally for /history.
MAX_HISTORY_MESSAGES = 40

# One turn's tool loop adds messages that are not history yet, so
# MAX_HISTORY_MESSAGES alone does not bound a request. ~40k tokens.
MAX_REQUEST_CHARS = 160_000

# Cutting back below the ceiling keeps the provider's cached prefix stable
# for several rounds; trimming to it would drop a message every round.
TRIM_TARGET_CHARS = 120_000

# Floor, so one huge tool result cannot erase the question it answered.
MIN_REQUEST_MESSAGES = 4

TOOL_RESULT_PREVIEW = 200

# Sent when a run can make no more tool calls, whichever ceiling it reached.
# The tools are withheld with it, so the round it buys can only be the answer.
LAST_REQUEST = """
{reason}, and this is the last thing you will be asked.

Write the full report now, from what you have already read. Say plainly what
you could not determine rather than filling it in: a report with a named gap
is worth something, and a guess dressed as a finding is worth less than
nothing.

If there was an edit you had not made yet, put it in the report as a fenced
unified diff — `--- a/path`, `+++ b/path`, `@@` hunks, real context lines. It
costs you no tool call and it is applied verbatim, so the change still lands.
A described change is not applied; a diff is.
"""

OUT_OF_BUDGET = (
    "That is the whole budget for this investigation — there are no more tool "
    "calls available"
)

OUT_OF_ROUNDS = (
    "You have used every tool call this investigation allows — there are no "
    "more available"
)

# What a provider calls a response the output-token ceiling cut short.
TRUNCATED_FINISH_REASONS = ("length", "max_tokens", "max_output_tokens")

# Continuations bought for one answer that hit that ceiling. A report needs
# one; a model still going after two is not writing one.
MAX_CONTINUATIONS = 2

CONTINUE_TRUNCATED = """
Your last message stopped mid-sentence: it hit the output limit. Continue it
from exactly where it broke off, and nothing else. Do not repeat a word of
what you already wrote, do not start the report over, do not apologize — the
two halves are joined verbatim, so anything else lands mid-sentence.
"""

# True approves, False denies, a string denies with a reason for the model.
# A `Correction` is the third answer: the call was wrong, not forbidden.
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool | str]]


class Correction(str):
    """A refusal the model can fix by calling again, differently.

    A plain string means a person said no and gave a reason, so the model is
    told not to retry. A policy that is describing a mistake — the wrong root,
    a missing argument — needs the opposite answer, and an unattended run has
    nobody to ask instead. Subclasses `str` so the callback protocol, and
    every policy that returns an ordinary string, are unchanged.
    """


PERMISSION_DENIED = (
    "Error: the user denied permission for this tool call. "
    "Do not retry it — adjust your approach or ask the user what to do."
)

NOT_ALLOWED_AS_CALLED = (
    "Error: that call is not allowed as written. {reason}\n"
    "The change itself was not refused: make the corrected call now."
)

# Arguments cut off mid-JSON by the output ceiling. Not a denial: telling the
# model not to retry ended runs with no change at all.
MALFORMED_ARGUMENTS = (
    "Error: the arguments for that call were not valid JSON — they were most "
    "likely cut off by the output limit. This was not a refusal. Make the "
    "call again, smaller: edit_file on the few lines that change costs a "
    "fraction of write_file on a whole file."
)


def truncated(response: CompletionResponse) -> bool:
    """Whether the output-token ceiling cut this response short."""
    return response.finish_reason in TRUNCATED_FINISH_REASONS


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


def pinned_indexes(messages: list[dict[str, Any]]) -> set[int]:
    """The system prompt, the first user message, and the newest one.

    The first user message is the brief — in a daemon run it carries the
    error, the rules and the report format — and the newest is what this
    round is answering. Dropping either makes the model answer
    conversationally, which costs a re-ask. Both sit at the front, so pinning
    them also keeps the cached prefix stable.
    """
    users = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    return {0, *users[:1], *users[-1:]}


def trim_request_messages(messages: list[dict[str, Any]]) -> None:
    """Drop the oldest rounds in place until the request fits the budget.

    Nothing is dropped below MAX_REQUEST_CHARS; past it the request is cut
    back to TRIM_TARGET_CHARS in one go, so the cached prefix survives. What
    goes is the tool rounds, oldest first, never what `pinned_indexes`
    protects — and cutting from the middle can strand a tool result, so the
    result is swept for orphans.
    """
    total = sum(message_size(message) for message in messages)
    if total <= MAX_REQUEST_CHARS:
        return
    pinned = pinned_indexes(messages)
    dropped: set[int] = set()
    for index, message in enumerate(messages):
        if total <= TRIM_TARGET_CHARS:
            break
        # A too-big request beats one the API rejects outright.
        if len(messages) - len(dropped) <= MIN_REQUEST_MESSAGES:
            break
        if index in pinned:
            continue
        dropped.add(index)
        total -= message_size(message)
    messages[:] = drop_orphan_tool_results(
        [message for index, message in enumerate(messages) if index not in dropped]
    )
    total = sum(message_size(message) for message in messages)
    if total > MAX_REQUEST_CHARS:
        log.warning(
            "request is %d chars after trimming to %d messages — the provider "
            "may reject it as too long",
            total, len(messages),
        )


def is_tool_context(message: dict[str, Any]) -> bool:
    """Whether a message is a tool call or its result rather than plain text."""
    return message.get("role") == "tool" or bool(message.get("tool_calls"))


def drop_orphan_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop tool results whose requesting assistant message is not here.

    The API rejects a tool result that no tool call precedes, and a trim can
    strand one anywhere — not only at the front.
    """
    kept: list[dict[str, Any]] = []
    answering = False
    for message in messages:
        if message.get("role") == "tool":
            if not answering:
                continue
        else:
            answering = bool(message.get("tool_calls"))
        kept.append(message)
    return kept


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
        max_rounds: int = MAX_TOOL_ROUNDS,
        cost_limit_usd: float = 0.0,
    ):
        self.config = config
        self.history: list[dict[str, Any]] = []
        self.registry = tools or default_registry()
        self.approve = approve
        self.max_rounds = max_rounds
        self.usage: dict[str, int] = {}
        # Whole-life spend. `usage` is cleared every turn, and one incident is
        # several: the analysis, a re-ask, the insistence, the repair.
        self.total_usage: dict[str, int] = {}
        self.cost_limit_usd = cost_limit_usd
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

    def spent_usd(self) -> float:
        """What this agent has cost so far, at the pricing table's rates."""
        return extract_usage(self.total_usage, self.model)[2]

    def exhausted(self) -> bool:
        """Whether this agent has spent everything one run is allowed.

        `max_rounds` bounds how many requests a run makes, not what they cost,
        and every round resends a growing prefix.
        """
        return bool(self.cost_limit_usd) and self.spent_usd() >= self.cost_limit_usd

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
        # Every round is billed, and the spend cap reads these numbers.
        self.usage = {}

        try:
            for _ in range(self.max_rounds):
                trim_request_messages(messages)
                response = await self.complete(messages, tools)
                accumulate_usage(self.usage, response.usage)
                accumulate_usage(self.total_usage, response.usage)

                if response.thinking:
                    thinking_parts.append(response.thinking)

                if not response.tool_calls:
                    content = await self.answer_in_full(messages, emit, response)
                    self.commit_turn(produced)
                    return CompletionResponse(
                        content=content,
                        thinking="".join(thinking_parts) or None,
                        usage=self.usage or None,
                        model=response.model,
                    )

                # Free: they read local files. The requests are what bill.
                async for _ in self.run_tools(emit, response):
                    pass

                if self.exhausted():
                    log.warning(
                        "spent $%.4f of the $%.2f this run is allowed; asking "
                        "for the report from what has been read already",
                        self.spent_usd(), self.cost_limit_usd,
                    )
                    return await self.answer_now(messages, emit, produced)

            # The round ceiling, banked like the spend ceiling: returning
            # `last_content` threw away everything a free run had read.
            log.warning(
                "used all %d tool rounds without an answer; asking for the "
                "report from what has been read already",
                self.max_rounds,
            )
            return await self.answer_now(messages, emit, produced, OUT_OF_ROUNDS)

        except Exception:
            self.rollback_user_message()
            raise

    async def answer_now(
        self,
        messages: list[dict[str, Any]],
        emit: Callable[[dict[str, Any]], None],
        produced: list[dict[str, Any]],
        reason: str = OUT_OF_BUDGET,
    ) -> CompletionResponse:
        """One last round with the tools withheld, to bank the work so far.

        Abandoning the run instead would pay for everything and file nothing.
        Withheld rather than discouraged, so it cannot loop again. `reason` is
        which ceiling was reached, which is all the two paths differ by.
        """
        emit({"role": "user", "content": LAST_REQUEST.format(reason=reason)})
        trim_request_messages(messages)
        response = await self.ask_without_tools(messages)
        content = await self.answer_in_full(messages, emit, response)
        self.commit_turn(produced)
        return CompletionResponse(
            content=content,
            usage=self.usage or None,
            model=response.model,
        )

    async def ask_without_tools(self, messages: list[dict[str, Any]]):
        """One request with no tools offered, billed to this run."""
        response = await self.complete(messages, [])
        accumulate_usage(self.usage, response.usage)
        accumulate_usage(self.total_usage, response.usage)
        return response

    async def answer_in_full(
        self,
        messages: list[dict[str, Any]],
        emit: Callable[[dict[str, Any]], None],
        response: CompletionResponse,
    ) -> str:
        """The answer, continued past the output ceiling if it stopped there.

        A report cut off mid-sentence passes every check the daemon makes —
        it is long enough, it has its headings — and is filed with its fix
        section half written. Worse in fix mode: the tokens the report ran out
        of are the ones the edit needed, so the run publishes an analysis and
        changes nothing.

        The continuation is asked with the tools withheld. This branch is only
        reached because the model was answering rather than calling, and a
        tool call mid-report would restart the answer.
        """
        emit({"role": "assistant", "content": response.content or ""})
        parts = [response.content or ""]
        for _ in range(MAX_CONTINUATIONS):
            if not truncated(response):
                return "".join(parts)
            log.warning(
                "the answer hit the %s-token output ceiling; asking it to "
                "continue from where it stopped",
                self.config.ai.max_tokens,
            )
            emit({"role": "user", "content": CONTINUE_TRUNCATED})
            trim_request_messages(messages)
            response = await self.ask_without_tools(messages)
            emit({"role": "assistant", "content": response.content or ""})
            parts.append(response.content or "")
        if truncated(response):
            log.warning(
                "the answer is still cut off after %d continuations; filing it "
                "as it stands. Raise ai.max_tokens for this provider.",
                MAX_CONTINUATIONS,
            )
        return "".join(parts)

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
            for _ in range(self.max_rounds):
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

    async def execute_tool(self, name: str, args: dict[str, Any] | None) -> str:
        if args is None:
            log.warning("tool_call name=%s had unparseable arguments", name)
            return MALFORMED_ARGUMENTS
        log.info("tool_call name=%s args=%s", name, args)
        args = self.registry.normalize(name, args)
        verdict = await self.permitted(name, args)
        if verdict is True:
            result = await self.registry.execute(name, args)
            log.info("tool_result name=%s len=%d", name, len(result))
        elif isinstance(verdict, Correction):
            result = NOT_ALLOWED_AS_CALLED.format(reason=verdict)
            log.info("tool_corrected name=%s: %s", name, verdict)
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
            *drop_orphan_tool_results(self.history[-MAX_HISTORY_MESSAGES:]),
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
        self.history = drop_orphan_tool_results(self.history)

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
    def parse_args(fn: dict) -> dict | None:
        """The call's arguments, or None when they did not parse.

        None rather than {}: an empty dict reaches the tool as a call with
        every argument missing, and the permission gate turns that into a
        refusal the model is told not to retry.
        """
        try:
            args = (
                json.loads(fn["arguments"])
                if isinstance(fn["arguments"], str)
                else fn["arguments"]
            )
        except json.JSONDecodeError:
            return None
        return args if isinstance(args, dict) else None
