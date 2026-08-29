"""A gateway's model list, read from the gateway itself.

A gateway fronts hundreds of models and takes on more every week, so there
is no catalogue worth shipping in the source — and no way to derive a real
model id from a display name. All three speak the OpenAI `/v1/models`
endpoint, which names each model and prices it, so setup asks the gateway
what it carries instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from openai import APIError, AsyncOpenAI

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 15.0
PER_MILLION = 1_000_000

# A bare model id carries no vendor, so the family it starts with stands in
# for one. Matched longest first, so "minimax" is not answered by "mi".
VENDOR_PREFIXES: dict[str, str] = {
    "claude": "anthropic",
    "deepseek": "deepseek",
    "gemini": "google",
    "gemma": "google",
    "glm": "z-ai",
    "gpt": "openai",
    "grok": "xai",
    "hunyuan": "tencent",
    "hy": "tencent",
    "kimi": "moonshot",
    "llama": "meta",
    "mimo": "xiaomi",
    "minimax": "minimax",
    "mistral": "mistralai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "qwen": "alibaba",
}

BY_LENGTH = tuple(sorted(VENDOR_PREFIXES.items(), key=lambda item: -len(item[0])))


@dataclass(frozen=True)
class CatalogEntry:
    """One model a gateway carries, at the price that gateway charges for it.

    The rates are USD per 1M tokens and are the gateway's own, which is not
    the vendor's list price: a reseller discounts some models and gives
    others away.
    """

    id: str
    vendor: str
    input: float | None
    output: float | None


def vendor_of(model_id: str, name: str = "") -> str:
    """Who makes the model, for grouping the list setup prints.

    Three sources, in descending order of how far they can be trusted: the
    `vendor/` prefix, the "Vendor: Model" display name, then the family the
    id starts with. Anything left over is grouped under "other" rather than
    dropped, so the list still adds up to what the gateway said it has.
    """
    if "/" in model_id:
        # OpenRouter marks its auto-updating aliases ~vendor/model-latest;
        # they are the same vendor and belong in the same group.
        return model_id.split("/", 1)[0].lower().lstrip("~")
    if ":" in name:
        return name.split(":", 1)[0].strip().lower()
    lowered = model_id.lower()
    for prefix, vendor in BY_LENGTH:
        if lowered.startswith(prefix):
            return vendor
    return "other"


def rate(pricing: dict, key: str) -> float | None:
    """One per-token price as USD per 1M tokens, or None if it is not quoted."""
    value = pricing.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value) * PER_MILLION
    except (TypeError, ValueError):
        return None


def entry_from(model) -> CatalogEntry:
    """One /v1/models row. Everything past `id` is an extension, so optional."""
    extra = getattr(model, "model_extra", None) or {}
    pricing = extra.get("pricing") or {}
    return CatalogEntry(
        id=model.id,
        vendor=vendor_of(model.id, extra.get("name") or ""),
        input=rate(pricing, "prompt"),
        output=rate(pricing, "completion"),
    )


async def read_catalog(base_url: str | None, api_key: str) -> tuple[CatalogEntry, ...]:
    client = AsyncOpenAI(
        api_key=api_key or "none", base_url=base_url, timeout=FETCH_TIMEOUT
    )
    try:
        page = await client.models.list()
        return tuple(entry_from(model) for model in page.data)
    finally:
        await client.close()


def fetch_catalog(base_url: str | None, api_key: str) -> tuple[CatalogEntry, ...]:
    """The gateway's models, or () when it will not say.

    Empty rather than raising: an unreachable catalogue costs a nicer
    prompt and nothing else — setup falls back to asking for an id — and
    that is not worth failing a setup over.
    """
    try:
        return asyncio.run(read_catalog(base_url, api_key))
    except (APIError, OSError, ValueError) as e:
        log.debug("No model catalogue from %s: %s", base_url, e)
        return ()


def by_vendor(
    entries: tuple[CatalogEntry, ...],
) -> dict[str, list[CatalogEntry]]:
    """The catalogue grouped by vendor, vendors and models both sorted."""
    groups: dict[str, list[CatalogEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.vendor, []).append(entry)
    for group in groups.values():
        group.sort(key=lambda entry: entry.id)
    return dict(sorted(groups.items()))
