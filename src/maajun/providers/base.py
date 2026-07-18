from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ProviderType(Enum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ProviderError(Exception):
    """A provider API call failed. The message is safe to show to the user."""


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


# Events yielded by stream_completion:
#   ("thinking", str)      — reasoning text delta
#   ("content", str)       — answer text delta
#   ("tool_calls", list)   — accumulated tool calls, emitted once at stream end
StreamChunk = tuple[str, Any]


class AIProvider(ABC):
    def __init__(self, config: dict[str, Any]):
        self.config = config

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

    @abstractmethod
    def get_provider_name(self) -> str:
        pass

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
