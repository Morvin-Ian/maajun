"""DeepSeek — OpenAI-compatible, with DSML tool-call markup to strip."""

import re

from .base import ProviderType
from .chat_completions import ChatCompletionsProvider

# DeepSeek sometimes emits its internal tool-call markup inside message
# content. It is not part of the answer, so it never reaches the user.
_DSML_RE = re.compile(r"<\|+DSML\|+>.*?</\|+DSML\|+tool_calls>", re.DOTALL)
_DSML_OPEN_RE = re.compile(r"<\|+DSML\|+[^>]*>")


class DeepSeekProvider(ChatCompletionsProvider):
    name = ProviderType.DEEPSEEK.value
    base_url = "https://api.deepseek.com"
    default_model = "deepseek-v4-flash"
    thinking_model = "deepseek-v4-pro"

    def clean_content(self, text: str) -> str:
        text = _DSML_RE.sub("", text)
        text = _DSML_OPEN_RE.sub("", text)
        return text.strip()
