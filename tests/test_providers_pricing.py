from datetime import UTC, datetime

from maajun.providers.pricing import compute_cost, extract_usage

# DeepSeek bills by the clock, so every cost assertion pins the hour.
# 2026-08-20 is a Thursday; 2026-08-22 a Saturday.
PEAK = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
OFF_PEAK = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def test_compute_cost_deepseek_v4_flash():
    cost = compute_cost(
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
        model="deepseek-v4-flash", at=PEAK,
    )
    # $0.44 input + $1.32 output = $1.76
    assert abs(cost - 1.76) < 0.001


def test_compute_cost_deepseek_v4_pro():
    cost = compute_cost(
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
        model="deepseek-v4-pro", at=PEAK,
    )
    # $1.32 input + $3.96 output = $5.28
    assert abs(cost - 5.28) < 0.001


def test_compute_cost_small_amount():
    cost = compute_cost(
        prompt_tokens=1000, completion_tokens=500,
        model="deepseek-v4-flash", at=PEAK,
    )
    # (1000/1M)*0.44 + (500/1M)*1.32 = 0.00044 + 0.00066 = 0.0011
    assert abs(cost - 0.0011) < 0.00001


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
    assert pro_cost > flash_cost
    assert pro_cost / flash_cost == 3


def test_an_unnamed_model_is_costed_at_the_dearest_rate():
    """A gateway that does not say what it ran must not be assumed cheap:
    the cap can survive over-reporting, not under-reporting."""
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    _, _, cost = extract_usage(usage, None)
    assert cost == compute_cost(1_000_000, 1_000_000, "unrecognised-model")
    assert cost > compute_cost(1_000_000, 1_000_000, "deepseek-v4-flash")


def test_the_fallback_rate_is_never_below_a_priced_model():
    """Derived from the table, so adding a dearer model moves it too."""
    from maajun.providers.pricing import DEFAULT_PRICING, PRICING

    for rates in PRICING.values():
        assert DEFAULT_PRICING["input"] >= rates["input"]
        assert DEFAULT_PRICING["output"] >= rates["output"]


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

    pricing.warned.discard("some-new-model")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert pricing.pricing_for("some-new-model") is pricing.DEFAULT_PRICING

    assert caplog.text.count("No pricing entry") == 1


def test_every_supported_provider_has_priced_defaults():
    """A provider whose own default model isn't priced can't be costed."""
    from maajun.providers.factory import ProviderFactory
    from maajun.providers.pricing import DEFAULT_PRICING, pricing_for

    for provider_type in ProviderFactory.get_supported_providers():
        provider_class = ProviderFactory.providers[provider_type]
        for model in (provider_class.default_model, provider_class.thinking_model):
            assert pricing_for(model) is not DEFAULT_PRICING, model


def test_the_default_rate_is_above_every_known_model():
    """An unpriced model must over-report, so the spend cap never overshoots."""
    from maajun.providers.pricing import DEFAULT_PRICING, PRICING

    for rates in PRICING.values():
        assert rates["input"] <= DEFAULT_PRICING["input"]
        assert rates["output"] <= DEFAULT_PRICING["output"]


def test_the_thinking_model_costs_more_than_the_default_one():
    """If these ever invert, thinking_mode has stopped being the premium path."""
    from maajun.providers.deepseek import DeepSeekProvider
    from maajun.providers.openai import OpenAIProvider

    for provider in (DeepSeekProvider, OpenAIProvider):
        cheap = compute_cost(1_000_000, 1_000_000, provider.default_model, at=PEAK)
        premium = compute_cost(1_000_000, 1_000_000, provider.thinking_model, at=PEAK)
        assert premium > cheap, provider.name


def test_every_shipped_model_has_a_price():
    """A provider default with no entry would silently fall back to guesswork."""
    from maajun.providers.deepseek import DeepSeekProvider
    from maajun.providers.openai import OpenAIProvider
    from maajun.providers.pricing import PRICING

    for provider in (DeepSeekProvider, OpenAIProvider):
        assert provider.default_model in PRICING
        assert provider.thinking_model in PRICING


# ---------------------------------------------------------------------------
# Cached prompt tokens
# ---------------------------------------------------------------------------


def test_cache_hits_are_billed_at_the_cache_hit_rate():
    """A resent prefix is a thirtieth of the price on DeepSeek; costing it in
    full made a long tool loop look several times dearer than the invoice."""
    fresh = compute_cost(1_000_000, 0, "deepseek-v4-flash", at=PEAK)
    cached = compute_cost(
        1_000_000, 0, "deepseek-v4-flash", cached_tokens=1_000_000, at=PEAK
    )
    assert abs(fresh - 0.44) < 0.001
    assert abs(cached - 0.014) < 0.001


def test_a_partly_cached_prompt_splits_between_the_two_rates():
    cost = compute_cost(
        1_000_000, 0, "deepseek-v4-flash", cached_tokens=750_000, at=PEAK
    )
    assert abs(cost - (0.25 * 0.44 + 0.75 * 0.014)) < 0.000_001


def test_more_cache_hits_than_prompt_tokens_does_not_go_negative():
    """Believing a provider that over-reports would refund the caller."""
    cost = compute_cost(
        1000, 0, "deepseek-v4-flash", cached_tokens=999_999, at=PEAK
    )
    assert cost == compute_cost(
        1000, 0, "deepseek-v4-flash", cached_tokens=1000, at=PEAK
    )
    assert cost > 0


def test_extract_usage_reads_the_providers_cache_hit_count():
    plain = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    hit = {**plain, "cached_tokens": 1_000_000}
    _, _, uncached_cost = extract_usage(plain, "deepseek-v4-flash")
    _, _, cached_cost = extract_usage(hit, "deepseek-v4-flash")
    assert cached_cost < uncached_cost


def test_a_provider_that_reports_no_cache_is_charged_in_full():
    """Absent is not zero, but both are billed the same: at the miss rate."""
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    _, _, cost = extract_usage(usage, "deepseek-v4-flash")
    assert cost == compute_cost(1_000_000, 0, "deepseek-v4-flash")


def test_every_model_has_a_cache_hit_rate_below_its_miss_rate():
    from maajun.providers.pricing import PRICING

    for model, rates in PRICING.items():
        assert rates["cached_input"] < rates["input"], model


# ---------------------------------------------------------------------------
# Peak and off-peak
# ---------------------------------------------------------------------------


def test_deepseek_off_peak_is_half_price():
    peak = compute_cost(1_000_000, 1_000_000, "deepseek-v4-flash", at=PEAK)
    off = compute_cost(1_000_000, 1_000_000, "deepseek-v4-flash", at=OFF_PEAK)
    assert abs(off - peak / 2) < 0.000_001


def test_the_peak_windows_are_read_in_utc():
    from maajun.providers.pricing import is_peak

    thursday = datetime(2026, 8, 20, tzinfo=UTC)
    peak_hours = {hour for hour in range(24) if is_peak(thursday.replace(hour=hour))}
    assert peak_hours == {1, 2, 3, 6, 7, 8, 9}


def test_weekends_are_off_peak_all_day():
    """Saturday and Sunday are exempt, even inside a peak window."""
    from maajun.providers.pricing import is_peak

    saturday = datetime(2026, 8, 22, 2, 0, tzinfo=UTC)
    assert is_peak(saturday.replace(day=20))  # the Thursday before, same hour
    assert not is_peak(saturday)


def test_the_weekend_is_read_in_beijing_time_not_utc():
    """DeepSeek states the exemption in UTC+8, which starts 16:00 UTC Friday."""
    from maajun.providers.pricing import is_peak

    # Monday 00:30 Beijing is still Sunday 16:30 UTC: off-peak.
    assert not is_peak(datetime(2026, 8, 23, 16, 30, tzinfo=UTC))
    # Monday 10:00 Beijing is Monday 02:00 UTC: a peak hour on a weekday.
    assert is_peak(datetime(2026, 8, 24, 2, 0, tzinfo=UTC))


def test_a_naive_timestamp_is_read_as_utc():
    from maajun.providers.pricing import is_peak

    assert is_peak(datetime(2026, 8, 20, 2, 0))


def test_openai_is_not_discounted_off_peak():
    """The schedule is DeepSeek's; applying it to OpenAI would under-report."""
    peak = compute_cost(1_000_000, 1_000_000, "gpt-4o", at=PEAK)
    off = compute_cost(1_000_000, 1_000_000, "gpt-4o", at=OFF_PEAK)
    assert peak == off


def test_an_unknown_model_is_never_discounted_off_peak():
    """Both the discount and the cache rate are claims about a model we could
    not identify; the cap must not take either on trust."""
    peak = compute_cost(1_000_000, 1_000_000, "who-knows", at=PEAK)
    off = compute_cost(1_000_000, 1_000_000, "who-knows", at=OFF_PEAK)
    assert peak == off


def test_the_vision_model_is_priced():
    from maajun.providers.pricing import DEFAULT_PRICING, PRICING, pricing_for

    assert pricing_for("deepseek-v4-flash-vision-exp", PEAK) is not DEFAULT_PRICING
    assert (
        pricing_for("deepseek-v4-flash-vision-exp", PEAK)
        == PRICING["deepseek-v4-flash-vision-exp"]
    )
