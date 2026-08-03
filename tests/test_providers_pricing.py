"""Tests for cost tracking."""

from maajun.providers.pricing import compute_cost, extract_usage


def test_compute_cost_deepseek_v4_flash():
    cost = compute_cost(
        prompt_tokens=1_000_000, completion_tokens=1_000_000, model="deepseek-v4-flash"
    )
    # $0.27 input + $1.10 output = $1.37
    assert abs(cost - 1.37) < 0.001


def test_compute_cost_deepseek_v4_pro():
    cost = compute_cost(
        prompt_tokens=1_000_000, completion_tokens=1_000_000, model="deepseek-v4-pro"
    )
    # $1.10 input + $4.40 output = $5.50
    assert abs(cost - 5.50) < 0.001


def test_compute_cost_small_amount():
    cost = compute_cost(prompt_tokens=1000, completion_tokens=500, model="deepseek-v4-flash")
    # (1000/1M)*0.27 + (500/1M)*1.10 = 0.00027 + 0.00055 = 0.00082
    assert abs(cost - 0.00082) < 0.00001


def test_compute_cost_zero_tokens():
    cost = compute_cost(prompt_tokens=0, completion_tokens=0)
    assert cost == 0.0


def test_compute_cost_unknown_model():
    cost = compute_cost(prompt_tokens=1_000_000, completion_tokens=1_000_000, model="unknown")
    assert cost > 0


def test_extract_usage_none():
    prompt, comp, cost = extract_usage(None)
    assert prompt == 0
    assert comp == 0
    assert cost == 0.0


def test_extract_usage_with_data():
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    prompt, comp, cost = extract_usage(usage)
    assert prompt == 100
    assert comp == 50
    assert cost > 0


def test_extract_usage_partial():
    usage = {"prompt_tokens": 100}
    prompt, comp, cost = extract_usage(usage)
    assert prompt == 100
    assert comp == 0
    assert cost > 0


def test_extract_usage_uses_model_pricing():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    _, _, flash_cost = extract_usage(usage, "deepseek-v4-flash")
    _, _, pro_cost = extract_usage(usage, "deepseek-v4-pro")
    assert flash_cost == compute_cost(1_000_000, 1_000_000, "deepseek-v4-flash")
    assert pro_cost == compute_cost(1_000_000, 1_000_000, "deepseek-v4-pro")
    assert pro_cost > flash_cost


def test_extract_usage_none_model_defaults_to_flash():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    _, _, cost = extract_usage(usage, None)
    assert cost == compute_cost(1_000_000, 1_000_000, "deepseek-v4-flash")


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def test_openai_models_are_priced():
    """Adding a provider without its prices silently moved the spend cap."""
    from maajun.providers.pricing import DEFAULT_PRICING, pricing_for

    for model in ("gpt-4o", "gpt-4o-mini"):
        assert pricing_for(model) is not DEFAULT_PRICING


def test_dated_model_names_resolve_to_their_family():
    from maajun.providers.pricing import PRICING, pricing_for

    assert pricing_for("gpt-4o-2024-08-06") == PRICING["gpt-4o"]


def test_longest_prefix_wins():
    """gpt-4o-mini must not resolve to gpt-4o, which is 16x the price."""
    from maajun.providers.pricing import PRICING, pricing_for

    assert pricing_for("gpt-4o-mini-2024-07-18") == PRICING["gpt-4o-mini"]


def test_unknown_model_warns_once(caplog):
    """A silent fallback misreports spend; a logged one is diagnosable."""
    import logging

    from maajun.providers import pricing

    pricing._warned.discard("some-new-model")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert pricing.pricing_for("some-new-model") is pricing.DEFAULT_PRICING

    assert caplog.text.count("No pricing entry") == 1


def test_every_supported_provider_has_priced_defaults():
    """A provider whose own default model isn't priced can't be costed."""
    from maajun.providers.factory import ProviderFactory
    from maajun.providers.pricing import DEFAULT_PRICING, pricing_for

    for provider_type in ProviderFactory.get_supported_providers():
        provider_class = ProviderFactory._providers[provider_type]
        for model in (provider_class.default_model, provider_class.thinking_model):
            assert pricing_for(model) is not DEFAULT_PRICING, model
