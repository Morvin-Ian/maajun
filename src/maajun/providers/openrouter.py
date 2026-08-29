from .base import ProviderType
from .chat_completions import ChatCompletionsProvider


class OpenRouterProvider(ChatCompletionsProvider):
    """OpenRouter, a gateway that fronts many vendors' models.

    No model catalogue and no default: a gateway's list changes weekly and
    names every model `vendor/model`, so `ai.model` has to be set rather
    than defaulted to something that may have been withdrawn.
    """

    name = ProviderType.OPENROUTER.value
    base_url = "https://openrouter.ai/api/v1"
    catalog_url = "https://openrouter.ai/models"
