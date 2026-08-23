from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone

log = logging.getLogger(__name__)


# USD per 1M tokens, list price, checked August 2026. Input is priced twice
# because that is how it is billed: fresh, and re-served from the provider's
# prefix cache. The tool loop resends a growing prefix every round, so the
# cache rate is what most input tokens cost.
#
# The DeepSeek rows are its peak rates; off-peak is half of them.
PRICING: dict[str, dict[str, float]] = {
    # https://api-docs.deepseek.com/quick_start/pricing
    "deepseek-v4-flash": {
        "input": 0.44, "cached_input": 0.014, "output": 1.32,
    },
    "deepseek-v4-flash-vision-exp": {
        "input": 0.44, "cached_input": 0.014, "output": 1.32,
    },
    "deepseek-v4-pro": {
        "input": 1.32, "cached_input": 0.044, "output": 3.96,
    },
    # https://developers.openai.com/api/docs/pricing
    "gpt-4o-mini": {
        "input": 0.15, "cached_input": 0.075, "output": 0.60,
    },
    "gpt-4o": {
        "input": 2.50, "cached_input": 1.25, "output": 10.00,
    },
}

RATE_KEYS = ("input", "cached_input", "output")

# Models on DeepSeek's peak/off-peak schedule, matched by prefix.
OFF_PEAK_DISCOUNTED = ("deepseek-",)
OFF_PEAK_MULTIPLIER = 0.5

# Peak windows as [start, end) hours of the UTC day.
PEAK_HOURS_UTC = ((1, 4), (6, 10))

# The weekend exemption is stated in Beijing time, so it starts 16:00 UTC Friday.
BILLING_TZ = timezone(timedelta(hours=8))


# What an unpriced model costs: the dearest thing maajun knows about, derived
# from the table so adding a pricier model moves it too.
DEFAULT_PRICING: dict[str, float] = {
    key: max(rates[key] for rates in PRICING.values()) for key in RATE_KEYS
}

# Warned about already, so it is reported once and not per incident.
warned: set[str] = set()


def is_peak(at: datetime) -> bool:
    """Whether `at` falls in a peak billing window."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    at = at.astimezone(UTC)
    if at.astimezone(BILLING_TZ).weekday() >= 5:  # Saturday or Sunday
        return False
    return any(start <= at.hour < end for start, end in PEAK_HOURS_UTC)


def on_off_peak_schedule(model: str) -> bool:
    return model.startswith(OFF_PEAK_DISCOUNTED)


def base_pricing(model: str | None) -> tuple[dict[str, float], str | None]:
    """Undiscounted rates for a model, and the table name they came from.

    Longest prefix first, so dated ids resolve to their family. Anything
    unrecognised costs at DEFAULT_PRICING: a cheap fallback would make the
    unknown case under-report, the one direction a spend cap must not fail in.
    """
    if not model:
        warn_once("(unnamed)")
        return DEFAULT_PRICING, None
    for name in sorted(PRICING, key=len, reverse=True):
        if model.startswith(name):
            return PRICING[name], name
    warn_once(model)
    return DEFAULT_PRICING, None


def pricing_for(model: str | None, at: datetime | None = None) -> dict[str, float]:
    """Rates for a model at a moment in time, halved if it is billed off-peak."""
    rates, name = base_pricing(model)
    if name is None or not on_off_peak_schedule(name):
        return rates
    if is_peak(at or datetime.now(UTC)):
        return rates
    return {key: value * OFF_PEAK_MULTIPLIER for key, value in rates.items()}


def warn_once(model: str) -> None:
    if model in warned:
        return
    warned.add(model)
    log.warning(
        "No pricing entry for model %r — costing it at $%.2f/$%.2f per 1M "
        "tokens, the dearest maajun knows. Reported spend and the daily cap "
        "will be approximate; add it to providers/pricing.py to fix.",
        model, DEFAULT_PRICING["input"], DEFAULT_PRICING["output"],
    )


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    model: str | None = None,
    cached_tokens: int = 0,
    at: datetime | None = None,
) -> float:
    """Cost in USD from token counts.

    `cached_tokens` is the part of `prompt_tokens` the provider served from
    its cache. It is clamped into the prompt: a provider that over-reports it
    would otherwise make the bill come out negative.
    """
    pricing = pricing_for(model, at)
    prompt = max(prompt_tokens, 0)
    cached = min(max(cached_tokens, 0), prompt)
    fresh = prompt - cached
    return round(
        (fresh / 1_000_000) * pricing["input"]
        + (cached / 1_000_000) * pricing["cached_input"]
        + (completion_tokens / 1_000_000) * pricing["output"],
        6,
    )


# Where each provider reports its cache hits, once flattened by a usage_of.
CACHE_HIT_KEYS = ("cached_tokens", "prompt_cache_hit_tokens")


def count(usage: dict[str, int], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return 0


def extract_usage(
    usage: dict[str, int] | None, model: str | None = None,
) -> tuple[int, int, float]:
    """(prompt_tokens, completion_tokens, cost) from a usage dict."""
    if not usage:
        return 0, 0, 0.0
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    cost = compute_cost(
        prompt, completion, model, cached_tokens=count(usage, CACHE_HIT_KEYS)
    )
    return prompt, completion, cost
