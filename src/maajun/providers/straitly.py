from .base import ProviderType
from .chat_completions import ChatCompletionsProvider


class StraitlyProvider(ChatCompletionsProvider):
    """Straitly, a gateway that fronts many vendors' models.

    Like OpenRouter: `vendor/model` ids and no default, so `ai.model` is
    required rather than guessed.
    """

    name = ProviderType.STRAITLY.value
    base_url = "https://api.straitly.ai/v1"
    catalog_url = "https://straitly.ai/models"
    model_example = "anthropic/claude-opus-5"
