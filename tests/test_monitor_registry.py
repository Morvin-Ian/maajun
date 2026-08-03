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


# -- Sentry -----------------------------------------------------------------


ISSUE = {
    "id": "4507",
    "title": "ValueError: invalid literal for int()",
    "culprit": "shop.views in checkout",
    "level": "error",
    "count": 12,
    "firstSeen": "2026-07-01T10:00:00Z",
    "lastSeen": "2026-07-18T09:30:00Z",
    "permalink": "https://acme.sentry.io/issues/4507/",
}


def _sentry():
    return SentryMonitor(token="t", org="acme", project="web")


def test_sentry_name_identifies_the_project():
    assert _sentry().name == "sentry:acme/web"


def test_sentry_event_carries_title_and_details():
    event = _sentry()._to_event(ISSUE)
    assert event.message == "Sentry: ValueError: invalid literal for int()"
    assert "shop.views in checkout" in event.details
    assert "Events: 12" in event.details
    assert ISSUE["permalink"] in event.details
    assert event.fingerprint == "4507"
    assert event.source == "sentry:acme/web"


def test_sentry_event_survives_a_sparse_issue():
    """The API omits fields on some issue kinds; that must not raise."""
    event = _sentry()._to_event({"id": "1"})
    assert event.message == "Sentry: Unknown issue"
    assert "Link:" not in event.details


def test_sentry_dedups_by_issue_id_across_polls():
    monitor = _sentry()
    assert monitor._item_id(ISSUE) == "4507"


def test_sentry_base_url_supports_self_hosted():
    monitor = SentryMonitor(
        token="t", org="acme", project="web", base_url="https://sentry.internal/",
    )
    assert monitor.base_url == "https://sentry.internal"


async def test_sentry_poll_converts_and_dedups(monkeypatch):
    monitor = _sentry()
    monkeypatch.setattr(monitor, "_fetch", lambda: _async_value([ISSUE]))

    first = await monitor.poll()
    assert len(first) == 1
    assert await monitor.poll() == []  # same id, second sighting


async def test_sentry_poll_survives_a_failing_api(monkeypatch, caplog):
    monitor = _sentry()

    async def boom():
        raise RuntimeError("sentry is down")

    monkeypatch.setattr(monitor, "_fetch", boom)
    assert await monitor.poll() == []
    assert "failed to fetch" in caplog.text


async def _async_value(value):
    return value
