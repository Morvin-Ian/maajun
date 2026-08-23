from typing import Any

from .anthropic import AnthropicProvider
from .base import AIProvider, ProviderType
from .deepseek import DeepSeekProvider
from .openai import OpenAIProvider
from .ox_alpha import OxAlphaProvider


class ProviderFactory:
    # Insertion order is what setup offers, cheapest first.
    providers = {
        ProviderType.OX_ALPHA: OxAlphaProvider,
        ProviderType.DEEPSEEK: DeepSeekProvider,
        ProviderType.OPENAI: OpenAIProvider,
        ProviderType.ANTHROPIC: AnthropicProvider,
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

    @classmethod
    def is_free(cls, provider: str) -> bool:
        for provider_type, provider_class in cls.providers.items():
            if provider_type.value == provider:
                return provider_class.free
        return False
