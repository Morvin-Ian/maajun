
from pydantic import BaseModel, Field, field_validator

from maajun.providers.base import ProviderType


class AIProviderConfig(BaseModel):
    provider: str = ProviderType.DEEPSEEK.value
    model: str | None = None  # None -> provider default
    api_key: str | None = None
    temperature: float = 0.3
    max_tokens: int = 4096
    thinking_mode: bool = False

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        valid_providers = [p.value for p in ProviderType]
        if v not in valid_providers:
            raise ValueError(f'Provider must be one of: {", ".join(valid_providers)}')
        return v


class Config(BaseModel):
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
