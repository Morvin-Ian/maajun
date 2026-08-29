from .base import ModelInfo, ProviderType
from .chat_completions import ChatCompletionsProvider


class OpenAIProvider(ChatCompletionsProvider):
    name = ProviderType.OPENAI.value
    base_url = None  # the SDK default, https://api.openai.com/v1
    default_model = "gpt-4o-mini"
    thinking_model = "gpt-4o"
    models = (
        ModelInfo("gpt-4o-mini", "The cheap tier; fine for most incidents."),
        ModelInfo("gpt-4o", "Stronger reasoning, and much dearer per token."),
    )
