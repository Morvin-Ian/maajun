"""Tests for provider selection and the shared OpenAI-compatible base."""

import pytest

from maajun.providers.base import ProviderType
from maajun.providers.deepseek import DeepSeekProvider
from maajun.providers.factory import ProviderFactory
from maajun.providers.openai import OpenAIProvider


def test_both_providers_are_supported():
    supported = ProviderFactory.get_supported_providers()
    assert ProviderType.DEEPSEEK in supported
    assert ProviderType.OPENAI in supported


@pytest.mark.parametrize("provider_type,expected", [
    (ProviderType.DEEPSEEK, DeepSeekProvider),
    (ProviderType.OPENAI, OpenAIProvider),
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


@pytest.mark.parametrize("provider_class", [DeepSeekProvider, OpenAIProvider])
def test_thinking_mode_selects_the_thinking_model(provider_class):
    plain = provider_class({"api_key": "k"})
    thinking = provider_class({"api_key": "k", "thinking_mode": True})
    assert plain.model == provider_class.default_model
    assert thinking.model == provider_class.thinking_model
    assert plain.model != thinking.model


@pytest.mark.parametrize("provider_class", [DeepSeekProvider, OpenAIProvider])
def test_explicit_model_wins_over_thinking_mode(provider_class):
    provider = provider_class({"api_key": "k", "model": "custom-1", "thinking_mode": True})
    assert provider.model == "custom-1"


def test_openai_does_not_strip_content():
    """DSML is a DeepSeek quirk; OpenAI content must pass through untouched."""
    text = "answer with <|DSML|> literal text"
    assert OpenAIProvider({"api_key": "k"}).clean_content(text) == text
    assert DeepSeekProvider({"api_key": "k"}).clean_content(text) != text
