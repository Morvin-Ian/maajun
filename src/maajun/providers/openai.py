"""OpenAI — the reference implementation of the wire protocol.

Note for readers: this module is `maajun.providers.openai`, which does not
shadow the `openai` SDK. Absolute imports resolve from sys.path, so a sibling
module is only reachable as `maajun.providers.openai`.

No base_url override (the SDK's default is correct) and no content quirks to
strip, so this is only model selection. Override either model with
`maajun config ai.model <name>` when a newer one is available.
"""

from .base import ProviderType
from .chat_completions import ChatCompletionsProvider


class OpenAIProvider(ChatCompletionsProvider):
    name = ProviderType.OPENAI.value
    base_url = None  # the SDK default, https://api.openai.com/v1
    default_model = "gpt-4o-mini"
    thinking_model = "gpt-4o"
