"""Tests for the monitor registry and the declarative [[monitor.instances]] wiring."""

import pytest

from maajun.config import Config, GitHubConfig, MonitorConfig, MonitorInstanceConfig, RepoConfig
from maajun.daemon import _build_monitors
from maajun.monitors import LogFileMonitor, MonitorRegistry, SentryMonitor


def test_known_types_cover_the_shipped_monitors():
    assert {"logfile", "github-actions", "sentry"} <= set(MonitorRegistry.known_types())


def test_create_builds_a_registered_type(tmp_path):
    monitor = MonitorRegistry.create("logfile", path=tmp_path / "a.log")
    assert isinstance(monitor, LogFileMonitor)


def test_create_rejects_unknown_type_with_the_known_ones_listed():
    with pytest.raises(ValueError, match="Unknown monitor type"):
        MonitorRegistry.create("logfle")


def _config(**monitor_kwargs) -> Config:
    return Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(**monitor_kwargs),
    )


def test_log_files_shorthand_attaches_to_the_first_repo(tmp_path):
    config = _config(log_files=[str(tmp_path / "a.log")])
    monitors, monitor_to_repo = _build_monitors(config, config.github.get_all_repos())

    assert len(monitors) == 1
    assert monitor_to_repo[monitors[0].name].repo == "owner/name"


def test_instances_build_registered_monitors(tmp_path):
    config = _config(instances=[
        MonitorInstanceConfig(type="logfile", path=str(tmp_path / "a.log")),
        MonitorInstanceConfig(type="sentry", token="t", org="acme", project="web"),
    ])
    monitors, _ = _build_monitors(config, config.github.get_all_repos())

    assert isinstance(monitors[0], LogFileMonitor)
    assert isinstance(monitors[1], SentryMonitor)


def test_instance_targeting_an_unconfigured_repo_still_builds(tmp_path, caplog):
    """Its events get analyzed; they just have no repo to open a PR against."""
    config = _config(instances=[
        MonitorInstanceConfig(type="logfile", path=str(tmp_path / "a.log"),
                              repo="other/repo"),
    ])
    monitors, monitor_to_repo = _build_monitors(config, config.github.get_all_repos())

    assert len(monitors) == 1
    assert monitor_to_repo == {}
    assert "not configured" in caplog.text


def test_unknown_instance_type_is_a_readable_error():
    """A config typo must not surface as a bare traceback at daemon startup."""
    config = _config(instances=[MonitorInstanceConfig(type="logfle", path="/tmp/a.log")])
    with pytest.raises(RuntimeError, match="Unknown monitor type"):
        _build_monitors(config, config.github.get_all_repos())


def test_bad_instance_setting_is_a_readable_error():
    config = _config(instances=[
        MonitorInstanceConfig(type="logfile", path="/tmp/a.log", nonsense=1),
    ])
    with pytest.raises(RuntimeError, match="Invalid settings for monitor type"):
        _build_monitors(config, config.github.get_all_repos())


def test_monitor_tuning_reaches_the_built_monitor(tmp_path):
    config = _config(
        log_files=[str(tmp_path / "a.log")],
        burst_threshold=4,
        burst_window_seconds=90.0,
        json_level_field="severity",
    )
    monitors, _ = _build_monitors(config, config.github.get_all_repos())

    monitor = monitors[0]
    assert monitor._burst_threshold == 4
    assert monitor._burst_window_seconds == 90.0
    assert monitor._json_level_field == "severity"


def test_github_actions_shorthand_uses_the_registry():
    config = _config(
        github_actions_token="ghp_x", github_actions_repos=["owner/name"],
    )
    monitors, monitor_to_repo = _build_monitors(config, config.github.get_all_repos())

    assert monitors[0].name == "gh-actions:owner/name"
    assert monitor_to_repo[monitors[0].name].repo == "owner/name"
