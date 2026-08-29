from types import SimpleNamespace

import pytest

from maajun.providers.catalog import (
    CatalogEntry,
    by_vendor,
    entry_from,
    fetch_catalog,
    rate,
    vendor_of,
)


def model(model_id, name=None, pricing=None):
    """A /v1/models row as the SDK hands it over: id, then extensions."""
    extra = {}
    if name is not None:
        extra["name"] = name
    if pricing is not None:
        extra["pricing"] = pricing
    return SimpleNamespace(id=model_id, model_extra=extra)


# ---------------------------------------------------------------------------
# Which vendor a model belongs to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id,expected", [
    ("anthropic/claude-opus-5", "anthropic"),
    ("openai/gpt-5.2", "openai"),
    ("z-ai/glm-5.3", "z-ai"),
])
def test_a_prefixed_id_names_its_own_vendor(model_id, expected):
    assert vendor_of(model_id) == expected


def test_an_alias_groups_with_the_vendor_it_aliases():
    """OpenRouter marks its rolling aliases with ~, and a ~anthropic group
    beside anthropic is two entries for one vendor."""
    assert vendor_of("~anthropic/claude-opus-latest") == "anthropic"


def test_a_bare_id_falls_back_to_the_display_name():
    assert vendor_of("hy4-preview", "Tencent: Hy4 preview") == "tencent"


@pytest.mark.parametrize("model_id,expected", [
    ("claude-opus-5-thinking-high", "anthropic"),
    ("gpt-5.6-sol", "openai"),
    ("gemini-3.6-flash", "google"),
    ("deepseek-v4-pro", "deepseek"),
    ("glm-5.3", "z-ai"),
    ("kimi-k2.6", "moonshot"),
    ("minimax-m3", "minimax"),
    ("mimo-v2.5-pro", "xiaomi"),
    ("qwen3.8-max", "alibaba"),
    ("hunyuan-hy3", "tencent"),
])
def test_a_bare_id_with_no_name_falls_back_to_the_family(model_id, expected):
    assert vendor_of(model_id) == expected


def test_the_longest_family_wins():
    """"minimax" starts with "mi", which xiaomi's "mimo" must not claim."""
    assert vendor_of("minimax-m2.7") == "minimax"


def test_an_unrecognised_id_is_grouped_rather_than_dropped():
    """The list has to add up to what the gateway said it carries."""
    assert vendor_of("something-nobody-has-heard-of") == "other"


# ---------------------------------------------------------------------------
# Reading one row
# ---------------------------------------------------------------------------


def test_a_price_is_converted_to_dollars_per_million_tokens():
    assert rate({"prompt": "0.000005"}, "prompt") == pytest.approx(5.0)


@pytest.mark.parametrize("pricing", [{}, {"prompt": None}, {"prompt": ""}, {"prompt": "n/a"}])
def test_a_missing_or_unreadable_price_is_none_not_zero(pricing):
    """Zero is a price a gateway really charges, so it cannot mean "unknown"."""
    assert rate(pricing, "prompt") is None


def test_a_row_without_extensions_still_yields_an_entry():
    """`id` is all /v1/models promises; the rest is an extension."""
    entry = entry_from(SimpleNamespace(id="gpt-5.2", model_extra=None))
    assert entry == CatalogEntry(id="gpt-5.2", vendor="openai", input=None, output=None)


def test_a_free_model_keeps_its_zero():
    entry = entry_from(model("glm-5.3-flash", pricing={"prompt": "0", "completion": "0"}))
    assert entry.input == 0 and entry.output == 0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_the_catalogue_is_grouped_and_sorted_both_ways():
    entries = tuple(
        entry_from(model(name))
        for name in ("openai/gpt-5.4", "anthropic/claude-opus-5", "openai/gpt-5.2")
    )
    groups = by_vendor(entries)
    assert list(groups) == ["anthropic", "openai"]
    assert [e.id for e in groups["openai"]] == ["openai/gpt-5.2", "openai/gpt-5.4"]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def test_an_unreachable_gateway_yields_no_catalogue_rather_than_failing(monkeypatch):
    """Setup falls back to asking for an id; it does not stop."""
    async def explode(base_url, api_key):
        raise OSError("no route to host")

    monkeypatch.setattr("maajun.providers.catalog.read_catalog", explode)
    assert fetch_catalog("https://gateway.example/v1", "k") == ()
