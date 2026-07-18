import pytest
from pydantic import ValidationError

from maajun.config import AIProviderConfig, Config, GitHubConfig
from maajun.providers.base import ProviderType


@pytest.mark.parametrize("provider", [p.value for p in ProviderType])
def test_valid_providers_accepted(provider):
    assert AIProviderConfig(provider=provider).provider == provider


def test_unknown_provider_rejected():
    with pytest.raises(ValidationError):
        AIProviderConfig(provider="groq")


def test_github_mode_validated():
    assert GitHubConfig(mode="fix").mode == "fix"
    with pytest.raises(ValidationError):
        GitHubConfig(mode="yolo")


def test_github_repo_shape_validated():
    assert GitHubConfig(repo="owner/name").repo == "owner/name"
    with pytest.raises(ValidationError):
        GitHubConfig(repo="not-a-repo")


def test_load_missing_file_gives_defaults(tmp_path):
    config = Config.load(tmp_path / "nope.toml")
    assert config.github.mode == "suggest"
    assert config.monitor.poll_interval == 30.0


def test_load_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[ai]
provider = "deepseek"
thinking_mode = true

[github]
repo = "morvin/webapp"
base_branch = "develop"
mode = "fix"

[monitor]
log_files = ["/var/log/app.log", "/var/log/worker.log"]
poll_interval = 10
"""
    )
    config = Config.load(path)
    assert config.ai.thinking_mode is True
    assert config.github.repo == "morvin/webapp"
    assert config.github.base_branch == "develop"
    assert config.github.mode == "fix"
    assert config.monitor.log_files == ["/var/log/app.log", "/var/log/worker.log"]
    assert config.monitor.poll_interval == 10.0


def test_load_rejects_invalid_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[github]\nmode = "yolo"\n')
    with pytest.raises(ValidationError):
        Config.load(path)
