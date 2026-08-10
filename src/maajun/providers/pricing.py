from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# USD per 1M tokens, list price, verified against the vendors' own pricing
# pages in August 2026. DeepSeek also bills cache *hits* at roughly a fiftieth
# of the input rate; that is not modelled here, because parse_response does not
# carry the prompt_cache_hit_tokens split through. Everything is therefore
# costed at the cache-miss rate, which over-reports rather than under-reports —
# the safe direction for a spend cap.
PRICING: dict[str, dict[str, float]] = {
    # https://api-docs.deepseek.com/quick_start/pricing
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
    # https://developers.openai.com/api/docs/pricing
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}


# What an unpriced model is costed at. Derived from the table rather than
# fixed, so it stays the most expensive thing maajun knows about: a flat
# $1.00/$3.00 was described as conservative but sat *below* gpt-4o, so an
# unrecognised premium model was under-costed and the daily cap overshot.
DEFAULT_PRICING: dict[str, float] = {
    "input": max(rates["input"] for rates in PRICING.values()),
    "output": max(rates["output"] for rates in PRICING.values()),
}

FALLBACK_MODEL = "deepseek-v4-flash"

# Models already warned about, so an unpriced model is reported once and not
# on every incident.
_warned: set[str] = set()


def pricing_for(model: str) -> dict[str, float]:
    """Rates for a model, longest prefix first so families resolve correctly."""
    for name in sorted(PRICING, key=len, reverse=True):
        if model.startswith(name):
            return PRICING[name]
    if model not in _warned:
        _warned.add(model)
        log.warning(
            "No pricing entry for model %r — costing it at $%.2f/$%.2f per 1M "
            "tokens. Reported spend and the daily cap will be approximate; add "
            "it to providers/pricing.py to fix.",
            model, DEFAULT_PRICING["input"], DEFAULT_PRICING["output"],
        )
    return DEFAULT_PRICING


def compute_cost(
    prompt_tokens: int, completion_tokens: int, model: str = FALLBACK_MODEL,
) -> float:
    """Cost in USD from token counts."""
    pricing = pricing_for(model)
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


def extract_usage(
    usage: dict[str, int] | None, model: str | None = None,
) -> tuple[int, int, float]:
    """(prompt_tokens, completion_tokens, cost) from a usage dict.

    Returns (0, 0, 0.0) when the provider reported no usage.
    """
    if not usage:
        return 0, 0, 0.0
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    return prompt, completion, compute_cost(prompt, completion, model or FALLBACK_MODEL)
