from typing import Any

from .anthropic import AnthropicProvider
from .bai import BAIProvider
from .base import AIProvider, ProviderType
from .deepseek import DeepSeekProvider
from .openai import OpenAIProvider
from .openrouter import OpenRouterProvider
from .straitly import StraitlyProvider


class ProviderFactory:
    # Insertion order is what setup offers, cheapest first.
    providers = {
        ProviderType.DEEPSEEK: DeepSeekProvider,
        ProviderType.OPENAI: OpenAIProvider,
        ProviderType.ANTHROPIC: AnthropicProvider,
        # Gateways last: they reach every model above, but only once
        # ai.model names one.
        ProviderType.OPENROUTER: OpenRouterProvider,
        ProviderType.STRAITLY: StraitlyProvider,
        ProviderType.BAI: BAIProvider,
    }

    @classmethod
    def create_provider(
        cls, provider_type: ProviderType, config: dict[str, Any]
    ) -> AIProvider:
        provider_class = cls.providers.get(provider_type)
        if not provider_class:
            raise ValueError(f"Unsupported provider: {provider_type}")
        return provider_class(config)

    @classmethod
    def get_supported_providers(cls) -> list[ProviderType]:
        return list(cls.providers)
