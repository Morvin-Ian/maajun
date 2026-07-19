import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from maajun.providers.base import ProviderType


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "maajun" / "config.toml"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(base).expanduser() / "maajun"


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


class GitHubConfig(BaseModel):
    repo: str = ""  # "owner/name"
    base_branch: str = "main"
    # "suggest": the PR contains only the analysis report.
    # "fix": the agent may also edit code inside the workspace.
    mode: str = "suggest"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("suggest", "fix"):
            raise ValueError('mode must be "suggest" or "fix"')
        return v

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, v: str) -> str:
        if v and v.count("/") != 1:
            raise ValueError('repo must be in "owner/name" form')
        return v


class MonitorConfig(BaseModel):
    log_files: list[str] = Field(default_factory=list)
    error_pattern: str = r"\b(ERROR|CRITICAL|FATAL)\b"
    poll_interval: float = 30.0

    sentry_auth_token: str = ""
    sentry_org: str = ""
    sentry_projects: list[str] = Field(default_factory=list)

    github_actions_token: str = ""
    github_actions_repos: list[str] = Field(default_factory=list)


class DaemonConfig(BaseModel):
    workdir: str = str(default_data_dir())
    notify_webhook_urls: list[str] = Field(default_factory=list)


class Config(BaseModel):
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config from a TOML file; missing file yields defaults."""
        path = path or default_config_path()
        if not path.exists():
            return cls()
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)
