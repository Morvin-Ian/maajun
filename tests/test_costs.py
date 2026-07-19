"""Tests for cost tracking."""

from maajun.costs import compute_cost, extract_usage


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
