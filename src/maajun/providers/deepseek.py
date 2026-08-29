import re

from .base import ModelInfo, ProviderType
from .chat_completions import ChatCompletionsProvider

# DeepSeek leaks its tool-call markup into message content sometimes.
DSML_RE = re.compile(r"<\|+DSML\|+>.*?</\|+DSML\|+tool_calls>", re.DOTALL)
DSML_OPEN_RE = re.compile(r"<\|+DSML\|+[^>]*>")


class DeepSeekProvider(ChatCompletionsProvider):
    name = ProviderType.DEEPSEEK.value
    base_url = "https://api.deepseek.com"
    default_model = "deepseek-v4-flash"
    thinking_model = "deepseek-v4-pro"
    models = (
        ModelInfo("deepseek-v4-flash", "Fast and cheap; fine for most incidents."),
        ModelInfo("deepseek-v4-pro", "More reasoning depth, for harder faults."),
        ModelInfo(
            "deepseek-v4-flash-vision-exp",
            "Experimental vision variant of flash, at the same price.",
        ),
    )

    def clean_content(self, text: str) -> str:
        text = DSML_RE.sub("", text)
        text = DSML_OPEN_RE.sub("", text)
        return text.strip()
