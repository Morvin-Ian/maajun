from .base import ProviderType
from .chat_completions import ChatCompletionsProvider


class OxAlphaProvider(ChatCompletionsProvider):
    """Ox Alpha, the free stealth model on OpenRouter.

    One model, no cheap/premium split, so thinking_mode changes nothing.
    Free only for the preview; when that ends it is pricing.py that changes.
    """

    name = ProviderType.OX_ALPHA.value
    base_url = "https://openrouter.ai/api/v1"
    default_model = "stealth/ox-alpha"
    thinking_model = "stealth/ox-alpha"
    free = True
