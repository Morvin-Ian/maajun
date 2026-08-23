import pytest

from maajun.providers.anthropic import AnthropicProvider
from maajun.providers.base import ProviderType
from maajun.providers.deepseek import DeepSeekProvider
from maajun.providers.factory import ProviderFactory
from maajun.providers.openai import OpenAIProvider
from maajun.providers.ox_alpha import OxAlphaProvider


def test_every_provider_is_supported():
    assert ProviderFactory.get_supported_providers() == [
        ProviderType.OX_ALPHA,
        ProviderType.DEEPSEEK,
        ProviderType.OPENAI,
        ProviderType.ANTHROPIC,
    ]


def test_the_free_provider_is_offered_first():
    """setup defaults to implemented[0], so the order is the recommendation."""
    first = ProviderFactory.get_supported_providers()[0]
    assert ProviderFactory.is_free(first.value)
    assert not any(
        ProviderFactory.is_free(p.value)
        for p in ProviderFactory.get_supported_providers()[1:]
    )


@pytest.mark.parametrize("provider_type,expected", [
    (ProviderType.OX_ALPHA, OxAlphaProvider),
    (ProviderType.DEEPSEEK, DeepSeekProvider),
    (ProviderType.OPENAI, OpenAIProvider),
    (ProviderType.ANTHROPIC, AnthropicProvider),
])
def test_factory_builds_each_provider(provider_type, expected):
    provider = ProviderFactory.create_provider(provider_type, {"api_key": "k"})
    assert isinstance(provider, expected)
    assert provider.get_provider_name() == provider_type.value


def test_openai_uses_the_sdk_default_endpoint():
    """A base_url of None lets the SDK point at api.openai.com."""
    assert OpenAIProvider({"api_key": "k"}).base_url is None


def test_deepseek_points_at_its_own_endpoint():
    assert "deepseek.com" in DeepSeekProvider({"api_key": "k"}).base_url


def test_base_url_can_be_overridden_for_a_gateway():
    provider = OpenAIProvider({"api_key": "k", "base_url": "https://gateway.internal/v1"})
    assert provider.base_url == "https://gateway.internal/v1"


@pytest.mark.parametrize(
    "provider_class", [DeepSeekProvider, OpenAIProvider, AnthropicProvider]
)
def test_thinking_mode_selects_the_thinking_model(provider_class):
    plain = provider_class({"api_key": "k"})
    thinking = provider_class({"api_key": "k", "thinking_mode": True})
    assert plain.model == provider_class.default_model
    assert thinking.model == provider_class.thinking_model
    assert plain.model != thinking.model


@pytest.mark.parametrize(
    "provider_class",
    [DeepSeekProvider, OpenAIProvider, AnthropicProvider, OxAlphaProvider],
)
def test_explicit_model_wins_over_thinking_mode(provider_class):
    provider = provider_class({"api_key": "k", "model": "custom-1", "thinking_mode": True})
    assert provider.model == "custom-1"


def test_openai_does_not_strip_content():
    """DSML is a DeepSeek quirk; OpenAI content must pass through untouched."""
    text = "answer with <|DSML|> literal text"
    assert OpenAIProvider({"api_key": "k"}).clean_content(text) == text
    assert DeepSeekProvider({"api_key": "k"}).clean_content(text) != text


def test_ox_runs_the_free_stealth_model_through_openrouter():
    provider = OxAlphaProvider({"api_key": "k"})
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.model == "stealth/ox-alpha"
    assert provider.free


def test_ox_has_no_premium_tier_to_switch_to():
    """One free model serves both modes, so thinking_mode changes nothing."""
    plain = OxAlphaProvider({"api_key": "k"})
    thinking = OxAlphaProvider({"api_key": "k", "thinking_mode": True})
    assert plain.model == thinking.model


def test_only_ox_is_marked_free():
    assert not ProviderFactory.is_free("deepseek")
    assert not ProviderFactory.is_free("anthropic")
    assert not ProviderFactory.is_free("nonexistent")
