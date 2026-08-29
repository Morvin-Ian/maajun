from .base import ProviderType
from .chat_completions import ChatCompletionsProvider


class BAIProvider(ChatCompletionsProvider):
    """BAI, a gateway that fronts many vendors' models.

    It names them plainly — `gpt-5.2`, not `openai/gpt-5.2` — so an id that
    misses the pricing table misses it outright, with no vendor prefix left
    to strip. No default, for the same reason as the other gateways.
    """

    name = ProviderType.BAI.value
    base_url = "https://api.b.ai/v1"
    catalog_url = "https://docs.b.ai"
    model_example = "gpt-5.2"
