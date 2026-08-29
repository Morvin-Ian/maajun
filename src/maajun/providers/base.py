from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ProviderType(Enum):
    """Declaration order is the order they are offered in, cheapest first."""

    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    STRAITLY = "straitly"


class ProviderError(Exception):
    """A provider API call failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class ModelInfo:
    """One model a provider offers, as `maajun setup` lists it.

    No prices here: those come from pricing.PRICING, so the catalogue and
    the spend cap cannot disagree about what a model costs.
    """

    id: str
    note: str


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class CompletionResponse:
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str = "stop"
    usage: dict[str, int] | None = None
    raw_response: Any = None
    thinking: str | None = None
    model: str | None = None


# Events yielded by stream_completion:
#   ("thinking", str)      — reasoning text delta
#   ("content", str)       — answer text delta
#   ("tool_calls", list)   — accumulated tool calls, emitted once at stream end
#   ("usage", dict)        — token counts, emitted once at stream end
# Agent.chat_stream consumes "tool_calls" and instead yields:
#   ("tool", str)          — one-line preview of an executed tool's result
StreamChunk = tuple[str, Any]


class AIProvider(ABC):
    name: str = ""
    base_url: str | None = None
    default_model: str = ""
    thinking_model: str = ""
    # Offered by `maajun setup`. Empty for a gateway, which has more models
    # than a list can carry — catalog_url is where to look them up instead.
    models: tuple[ModelInfo, ...] = ()
    catalog_url: str = ""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.tools_key: tuple[str, ...] | None = None
        self.tools_prepared: list[dict[str, Any]] | None = None
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url") or self.base_url
        self.model = config.get("model") or (
            self.thinking_model
            if config.get("thinking_mode") and self.thinking_model
            else self.default_model
        )

    def get_provider_name(self) -> str:
        return self.name

    @abstractmethod
    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> CompletionResponse:
        pass

    @abstractmethod
    def stream_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """Yield StreamChunk events as they arrive.

        Text deltas are yielded immediately; if the model requests tools,
        a single ("tool_calls", list) event is yielded after the stream ends.
        """

    @abstractmethod
    async def validate_credentials(self) -> bool:
        pass

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release any held resources (e.g. HTTP clients). Override if needed."""

    def prepared_tools(
        self, tools: list[ToolDefinition] | None
    ) -> list[dict[str, Any]] | None:
        """The wire form of a tool list, rebuilt only when the set changes.

        Keyed on the names, not id(tools): CPython reuses freed addresses, so
        an id cache could serve one list's entry for another. Without it the
        same set is re-prepared on every round of the tool loop.
        """
        if not tools:
            return None
        key = tuple(tool.name for tool in tools)
        if key != self.tools_key:
            self.tools_key = key
            self.tools_prepared = self.prepare_tools(tools)
        return self.tools_prepared

    def prepare_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]
