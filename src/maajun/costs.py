"""Cost tracking for AI provider usage."""

# DeepSeek pricing per 1M tokens (USD) — as of July 2026
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 0.27, "output": 1.10},
    "deepseek-v4-pro": {"input": 1.10, "output": 4.40},
}

DEFAULT_PRICING: dict[str, float] = {"input": 1.00, "output": 3.00}


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    model: str = "deepseek-v4-flash",
) -> float:
    """Compute cost in USD from token counts."""
    pricing = DEEPSEEK_PRICING.get(model, DEFAULT_PRICING)
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


def extract_usage(
    usage: dict[str, int] | None,
    model: str | None = None,
) -> tuple[int, int, float]:
    """Extract (prompt_tokens, completion_tokens, cost) from usage dict.

    Returns (0, 0, 0.0) if usage is None.
    """
    if not usage:
        return 0, 0, 0.0

    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    cost = compute_cost(prompt, completion, model or "deepseek-v4-flash")
    return prompt, completion, cost
