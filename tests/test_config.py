import pytest
from pydantic import ValidationError

from maajun.config import AIProviderConfig
from maajun.providers.base import ProviderType


@pytest.mark.parametrize("provider", [p.value for p in ProviderType])
def test_valid_providers_accepted(provider):
    assert AIProviderConfig(provider=provider).provider == provider


def test_unknown_provider_rejected():
    with pytest.raises(ValidationError):
        AIProviderConfig(provider="groq")
