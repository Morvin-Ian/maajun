from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from maajun.providers.bai import BAIProvider
from maajun.providers.openrouter import OpenRouterProvider
from maajun.providers.straitly import StraitlyProvider

GATEWAYS = (OpenRouterProvider, StraitlyProvider, BAIProvider)


def listing_provider(cls, config, models_list):
    """A gateway whose models.list is `models_list` and whose chat call fails."""
    provider = cls(config)

    async def create(**kwargs):
        raise AssertionError("a gateway with no model still sent a completion")

    provider.client = SimpleNamespace(
        models=SimpleNamespace(list=models_list),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    return provider


@pytest.mark.parametrize("cls", GATEWAYS)
async def test_a_gateway_key_is_checked_against_the_model_list(cls):
    """Regression: setup stores the key before asking for a model, so a
    gateway validated with model="" and every good key was called bad."""
    called = []

    async def models_list():
        called.append(True)
        return SimpleNamespace(data=[])

    provider = listing_provider(cls, {"api_key": "k"}, models_list)

    assert await provider.validate_credentials() is True
    assert called


@pytest.mark.parametrize("cls", GATEWAYS)
async def test_a_gateway_with_a_model_still_checks_that_model(cls):
    """Once ai.model names one, the key has to reach that, not just the list."""
    provider = cls({"api_key": "k", "model": "openai/gpt-5.2"})
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=None)])

    async def models_list():
        raise AssertionError("the model list stood in for a named model")

    provider.client = SimpleNamespace(
        models=SimpleNamespace(list=models_list),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    assert await provider.validate_credentials() is True
    assert seen["model"] == "openai/gpt-5.2"


@pytest.mark.parametrize("cls", GATEWAYS)
async def test_a_rejected_gateway_key_is_still_reported(cls):
    async def models_list():
        request = httpx.Request("GET", "https://gateway.example/v1/models")
        raise APIStatusError(
            "unauthorized", response=httpx.Response(401, request=request), body=None
        )

    provider = listing_provider(cls, {"api_key": "k"}, models_list)

    assert await provider.validate_credentials() is False


async def test_bai_points_at_its_own_endpoint():
    assert BAIProvider({"api_key": "k"}).base_url == "https://api.b.ai/v1"


@pytest.mark.parametrize("cls", GATEWAYS)
def test_every_gateway_offers_an_id_in_its_own_naming(cls):
    """setup shows this as the example, so a vendor prefix where the gateway
    wants none sends people to a model id that does not resolve."""
    assert cls.model_example
    assert cls.catalog_url
    assert not cls.default_model


def test_bai_names_models_without_a_vendor_prefix():
    assert "/" not in BAIProvider.model_example

