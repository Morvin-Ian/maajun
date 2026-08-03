import pytest
from pydantic import ValidationError

from maajun.config import AIProviderConfig, Config, GitHubConfig, RepoConfig
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


@pytest.mark.parametrize("bad", ["/name", "owner/", "a/b/c"])
def test_github_repo_rejects_malformed_slugs(bad):
    # Validation now shares utils.is_valid_repo, which rejects leading/trailing
    # slashes — not just a wrong slash count.
    with pytest.raises(ValidationError):
        GitHubConfig(repo=bad)


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


# ---------------------------------------------------------------------------
# save() round-trip
# ---------------------------------------------------------------------------


def test_save_preserves_user_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[ai]\nprovider = "deepseek"  # keep me\ntemperature = 0.3\n'
        '\n[github]\nrepo = "owner/name"\nmode = "suggest"\n'
    )
    config = Config.load(path)
    config.set("github.mode", "fix")
    config.save()

    text = path.read_text()
    assert "# keep me" in text  # inline comment survived
    assert 'mode = "fix"' in text
    assert Config.load(path).github.mode == "fix"


def test_save_then_load_is_stable(tmp_path):
    path = tmp_path / "config.toml"
    config = Config()
    config.github.repo = "owner/name"
    config.monitor.log_files = ["/a.log", "/b.log"]
    config.save(path)

    reloaded = Config.load(path)
    assert reloaded.github.repo == "owner/name"
    assert reloaded.monitor.log_files == ["/a.log", "/b.log"]


def test_save_multi_repo_drops_legacy_scalar(tmp_path):
    path = tmp_path / "config.toml"
    config = Config()
    config.add_repo(RepoConfig(repo="a/b", mode="fix", log_files=["/x.log"]))
    config.save(path)

    text = path.read_text()
    assert "[[github.repos]]" in text
    reloaded = Config.load(path)
    assert [rc.repo for rc in reloaded.github.repos] == ["a/b"]
    assert reloaded.github.repos[0].log_files == ["/x.log"]


# ---------------------------------------------------------------------------
# set()/get() dot-notation
# ---------------------------------------------------------------------------


def test_set_coerces_types_and_lists():
    config = Config()
    config.set("monitor.poll_interval", "12")
    assert config.monitor.poll_interval == 12.0
    config.set("ai.thinking_mode", "true")
    assert config.ai.thinking_mode is True
    config.set("monitor.log_files", "/a.log, /b.log")
    assert config.monitor.log_files == ["/a.log", "/b.log"]


def test_set_invalid_value_raises_value_error():
    config = Config()
    with pytest.raises(ValueError):
        config.set("ai.provider", "groq")
    with pytest.raises(ValueError):
        config.set("github.mode", "yolo")


def test_set_mode_propagates_to_repos():
    config = Config()
    config.add_repo(RepoConfig(repo="a/b", mode="suggest"))
    config.set("github.mode", "fix")
    assert all(rc.mode == "fix" for rc in config.github.repos)


def test_get_unknown_key_raises():
    config = Config()
    with pytest.raises(ValueError):
        config.get("github.nonsense")
    with pytest.raises(ValueError):
        config.get("bogus.field")


def test_get_masks_secrets():
    config = Config()
    config.daemon.email.password = "hunter2"
    assert config.get("daemon.email.password") == "***"
    assert config.get("daemon.email.smtp_host") == ""


# ---------------------------------------------------------------------------
# add_repo()
# ---------------------------------------------------------------------------


def test_add_repo_migrates_legacy_single_repo():
    config = Config()
    config.github.repo = "owner/first"
    config.github.mode = "fix"
    config.add_repo(RepoConfig(repo="owner/second"))

    assert config.github.repo == ""
    repos = {rc.repo: rc for rc in config.github.repos}
    assert set(repos) == {"owner/first", "owner/second"}
    assert repos["owner/first"].mode == "fix"


def test_add_repo_replaces_duplicate():
    config = Config()
    config.add_repo(RepoConfig(repo="a/b", mode="suggest"))
    config.add_repo(RepoConfig(repo="a/b", mode="fix"))
    assert len(config.github.repos) == 1
    assert config.github.repos[0].mode == "fix"


def test_render_config_shows_single_repo():
    from maajun.config import MonitorConfig, render_config

    config = Config(
        ai=AIProviderConfig(provider="deepseek"),
        github=GitHubConfig(repo="owner/name", mode="fix"),
        monitor=MonitorConfig(log_files=["/var/log/app.log"]),
    )
    out = render_config(config)
    assert 'provider = [green]"deepseek"' in out
    assert 'repo = [green]"owner/name"' in out
    assert 'mode = [green]"fix"' in out
    assert "/var/log/app.log" in out


def test_render_config_lists_multi_repos():
    from maajun.config import render_config

    config = Config(github=GitHubConfig(repos=[
        RepoConfig(repo="a/b"), RepoConfig(repo="c/d", mode="fix"),
    ]))
    out = render_config(config)
    assert "github.repos" in out
    assert 'repo = [green]"a/b"' in out
    assert 'repo = [green]"c/d"' in out


def test_render_config_marks_unconfigured_repo():
    from maajun.config import render_config

    out = render_config(Config())
    assert "(not configured)" in out


# ---------------------------------------------------------------------------
# Monitor tuning round-trips through save/load
# ---------------------------------------------------------------------------


def test_monitor_tuning_survives_save_and_reload(tmp_path):
    """Regression: `maajun config monitor.<field>` reported success but the
    save path only wrote four keys, silently dropping everything else."""
    path = tmp_path / "config.toml"
    config = Config.load(path)
    config.set("monitor.burst_threshold", "5")
    config.set("monitor.burst_window_seconds", "120")
    config.set("monitor.json_level_field", "level")
    config.set("monitor.json_level_values", "error,fatal")
    config.set("monitor.use_watchdog", "true")
    config.save(path)

    reloaded = Config.load(path)
    assert reloaded.monitor.burst_threshold == 5
    assert reloaded.monitor.burst_window_seconds == 120
    assert reloaded.monitor.json_level_field == "level"
    assert reloaded.monitor.json_level_value_set == frozenset({"error", "fatal"})
    assert reloaded.monitor.use_watchdog is True


def test_default_monitor_tuning_is_not_written(tmp_path):
    """An untouched config stays readable — no wall of default tuning keys."""
    path = tmp_path / "config.toml"
    Config.load(path).save(path)

    text = path.read_text()
    assert "burst_threshold" not in text
    assert "json_level_field" not in text
    assert "use_watchdog" not in text


def test_monitor_instances_round_trip(tmp_path):
    from maajun.config import MonitorInstanceConfig

    path = tmp_path / "config.toml"
    config = Config.load(path)
    config.monitor.instances = [
        MonitorInstanceConfig(type="logfile", path="/var/log/a.log"),
        MonitorInstanceConfig(type="sentry", repo="owner/name", org="acme",
                              project="web"),
    ]
    config.save(path)

    reloaded = Config.load(path)
    assert [i.type for i in reloaded.monitor.instances] == ["logfile", "sentry"]
    assert reloaded.monitor.instances[0].monitor_kwargs() == {"path": "/var/log/a.log"}
    assert reloaded.monitor.instances[1].repo == "owner/name"
    assert reloaded.monitor.instances[1].monitor_kwargs() == {
        "org": "acme", "project": "web",
    }


def test_json_level_values_parse_to_a_lowercased_set():
    config = Config()
    config.set("monitor.json_level_values", " Error , FATAL ,, warn ")
    assert config.monitor.json_level_value_set == frozenset({"error", "fatal", "warn"})


def test_logfile_kwargs_match_the_monitor_signature():
    """The wiring dict must stay in step with LogFileMonitor's constructor."""
    import inspect

    from maajun.monitors import LogFileMonitor

    accepted = set(inspect.signature(LogFileMonitor.__init__).parameters)
    assert set(Config().monitor.logfile_kwargs()) <= accepted
