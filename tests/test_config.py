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


def test_repo_mode_validated():
    assert RepoConfig(mode="fix").mode == "fix"
    with pytest.raises(ValidationError):
        RepoConfig(mode="yolo")


def test_repo_shape_validated():
    assert RepoConfig(repo="owner/name").repo == "owner/name"
    with pytest.raises(ValidationError):
        RepoConfig(repo="not-a-repo")


@pytest.mark.parametrize("bad", ["/name", "owner/", "a/b/c"])
def test_repo_rejects_malformed_slugs(bad):
    # Validation shares utils.is_valid_repo, which rejects leading/trailing
    # slashes — not just a wrong slash count.
    with pytest.raises(ValidationError):
        RepoConfig(repo=bad)


def test_load_missing_file_gives_defaults(tmp_path):
    config = Config.load(tmp_path / "nope.toml")
    assert config.github.repos == []
    assert config.monitor.poll_interval == 30.0


def test_load_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[ai]
provider = "deepseek"
thinking_mode = true

[[github.repos]]
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
    assert [rc.repo for rc in config.github.repos] == ["morvin/webapp"]
    assert config.github.repos[0].base_branch == "develop"
    assert config.github.repos[0].mode == "fix"
    assert config.monitor.log_files == ["/var/log/app.log", "/var/log/worker.log"]
    assert config.monitor.poll_interval == 10.0


def test_load_rejects_invalid_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[github.repos]]\nrepo = "a/b"\nmode = "yolo"\n')
    with pytest.raises(ValidationError):
        Config.load(path)


# ---------------------------------------------------------------------------
# save() round-trip
# ---------------------------------------------------------------------------


def test_save_preserves_user_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[ai]\nprovider = "deepseek"  # keep me\ntemperature = 0.3\n'
        '\n[[github.repos]]\nrepo = "owner/name"\nmode = "suggest"\n'
    )
    config = Config.load(path)
    config.set("github.mode", "fix")
    config.save()

    text = path.read_text()
    assert "# keep me" in text  # inline comment survived
    assert 'mode = "fix"' in text
    assert Config.load(path).github.repos[0].mode == "fix"


def test_save_then_load_is_stable(tmp_path):
    path = tmp_path / "config.toml"
    config = Config()
    config.add_repo(RepoConfig(repo="owner/name"))
    config.monitor.log_files = ["/a.log", "/b.log"]
    config.save(path)

    reloaded = Config.load(path)
    assert [rc.repo for rc in reloaded.github.repos] == ["owner/name"]
    assert reloaded.monitor.log_files == ["/a.log", "/b.log"]


def test_save_writes_an_array_of_tables(tmp_path):
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


# ---------------------------------------------------------------------------
# add_repo()
# ---------------------------------------------------------------------------


def test_add_repo_appends_without_disturbing_the_first():
    config = Config()
    config.add_repo(RepoConfig(repo="owner/first", mode="fix"))
    config.add_repo(RepoConfig(repo="owner/second"))

    assert [rc.repo for rc in config.github.repos] == ["owner/first", "owner/second"]
    assert config.github.repos[0].mode == "fix"


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
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name", mode="fix")]),
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
    assert "no repositories" in out
    assert "maajun add-repo" in out


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
    config.save(path)

    reloaded = Config.load(path)
    assert reloaded.monitor.burst_threshold == 5
    assert reloaded.monitor.burst_window_seconds == 120
    assert reloaded.monitor.json_level_field == "level"
    assert reloaded.monitor.json_level_value_set == frozenset({"error", "fatal"})


def test_default_monitor_tuning_is_not_written(tmp_path):
    """An untouched config stays readable — no wall of default tuning keys."""
    path = tmp_path / "config.toml"
    Config.load(path).save(path)

    text = path.read_text()
    assert "burst_threshold" not in text
    assert "json_level_field" not in text


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


def test_load_and_save_accept_a_string_path(tmp_path):
    """The signature said Path, so a str crashed on path.exists()."""
    path = str(tmp_path / "config.toml")
    config = Config.load(path)
    config.add_repo(RepoConfig(repo="acme/webapp"))
    config.save(path)

    assert [rc.repo for rc in Config.load(path).github.repos] == ["acme/webapp"]


def test_base_url_round_trips(tmp_path):
    """Documented as the way to point at an OpenAI-compatible gateway."""
    path = tmp_path / "config.toml"
    config = Config()
    config.ai.base_url = "https://gateway.internal/v1"
    config.save(path)

    assert 'base_url = "https://gateway.internal/v1"' in path.read_text()
    assert Config.load(path).ai.base_url == "https://gateway.internal/v1"


def test_base_url_is_absent_when_unset(tmp_path):
    path = tmp_path / "config.toml"
    Config().save(path)

    # The key, not the word: paths in this file can contain anything.
    assert "base_url =" not in path.read_text()
    assert Config.load(path).ai.base_url is None


def test_base_url_is_settable_by_dot_notation(tmp_path):
    config = Config()
    config.set("ai.base_url", "https://gateway.internal/v1")

    assert config.ai.base_url == "https://gateway.internal/v1"
    assert config.get("ai.base_url") == "https://gateway.internal/v1"


# ---------------------------------------------------------------------------
# Per-repo set()/get()
# ---------------------------------------------------------------------------


def two_repos() -> Config:
    return Config(github=GitHubConfig(repos=[
        RepoConfig(repo="acme/api", base_branch="main", mode="suggest"),
        RepoConfig(repo="acme/web", base_branch="develop", mode="fix"),
    ]))


def test_a_github_field_with_no_repo_applies_to_every_repo():
    """Regression: only mode cascaded, so base_branch silently did nothing."""
    config = two_repos()
    config.set("github.base_branch", "trunk")

    assert [rc.base_branch for rc in config.github.repos] == ["trunk", "trunk"]


def test_test_command_cascades_too():
    config = two_repos()
    config.set("github.test_command", "pytest -q")

    assert [rc.test_command for rc in config.github.repos] == ["pytest -q", "pytest -q"]


def test_repo_scoped_set_touches_only_that_repo():
    config = two_repos()
    config.set("github.test_command", "pytest -q", "acme/web")

    assert config.github.repos[0].test_command == ""
    assert config.github.repos[1].test_command == "pytest -q"


def test_repo_scoped_get_reads_that_repos_value():
    config = two_repos()

    assert config.get("github.base_branch", "acme/web") == "develop"
    assert config.get("github.mode", "acme/api") == "suggest"


def test_repo_scoped_set_validates_the_value():
    config = two_repos()
    with pytest.raises(ValueError, match='mode must be "suggest" or "fix"'):
        config.set("github.mode", "yolo", "acme/api")


def test_repo_scoped_set_reaches_per_repo_only_fields():
    config = two_repos()
    config.set("github.log_files", "/a.log,/b.log", "acme/api")

    assert config.github.repos[0].log_files == ["/a.log", "/b.log"]
    assert config.github.repos[1].log_files == []


def test_repo_scoped_set_on_an_unknown_repo_says_how_to_add_it():
    config = two_repos()
    with pytest.raises(ValueError, match="add-repo other/thing"):
        config.set("github.mode", "fix", "other/thing")


def test_repo_scoped_set_rejects_a_non_github_key():
    config = two_repos()
    with pytest.raises(ValueError, match="github.\\* keys only"):
        config.set("monitor.poll_interval", "10", "acme/api")


def test_repo_scoped_set_rejects_a_field_that_is_not_per_repo():
    config = two_repos()
    with pytest.raises(ValueError, match="Unknown per-repo field: poll_interval"):
        config.set("github.poll_interval", "10", "acme/api")


def test_setting_github_repo_is_not_a_thing():
    """There is no top-level repo any more; add-repo owns which repos exist."""
    config = two_repos()
    with pytest.raises(ValueError, match="Unknown field"):
        config.set("github.repo", "someone/else")


def test_a_per_repo_key_with_no_repos_says_add_one():
    config = Config()
    with pytest.raises(ValueError, match="No repositories are configured"):
        config.set("github.mode", "fix")


def test_get_a_per_repo_key_names_each_repo_when_they_differ():
    config = two_repos()
    assert config.get("github.mode") == "acme/api=suggest, acme/web=fix"


def test_get_a_per_repo_key_is_bare_with_one_repo():
    config = Config(github=GitHubConfig(repos=[RepoConfig(repo="acme/api", mode="fix")]))
    assert config.get("github.mode") == "fix"


def test_setting_github_repos_explains_itself():
    config = Config()
    with pytest.raises(ValueError, match="add-repo"):
        config.set("github.repos", "a/b,c/d")


def test_add_repo_updates_in_place_and_keeps_the_order():
    """The first repo owns global monitor.log_files, so order is load-bearing."""
    config = two_repos()
    config.add_repo(RepoConfig(repo="acme/api", mode="fix"))

    assert [rc.repo for rc in config.github.repos] == ["acme/api", "acme/web"]
    assert config.github.repos[0].mode == "fix"


def test_add_repo_appends_a_genuinely_new_repo():
    config = two_repos()
    config.add_repo(RepoConfig(repo="acme/jobs"))

    assert [rc.repo for rc in config.github.repos] == [
        "acme/api", "acme/web", "acme/jobs",
    ]


# ---------------------------------------------------------------------------
# The old single-repo format is rejected, not ignored
# ---------------------------------------------------------------------------


def test_legacy_single_repo_config_is_rejected_with_the_fix(tmp_path):
    """Pydantic drops unknown keys, so this would otherwise load as "no repo
    configured" and the daemon would quietly write reports instead of PRs."""
    from maajun.config import ConfigError

    path = tmp_path / "config.toml"
    path.write_text('[github]\nrepo = "acme/api"\nbase_branch = "main"\n')

    with pytest.raises(ConfigError) as exc:
        Config.load(path)
    message = str(exc.value)
    assert "old single-repo format" in message
    assert "maajun add-repo acme/api" in message
    assert str(path) in message


def test_legacy_local_mode_config_is_told_to_delete_the_keys(tmp_path):
    """repo = "" was how the old format spelled local mode; there is no repo to
    add, so the advice has to differ."""
    from maajun.config import ConfigError

    path = tmp_path / "config.toml"
    path.write_text('[github]\nrepo = ""\nmode = "suggest"\n')

    with pytest.raises(ConfigError, match="Delete those keys"):
        Config.load(path)


@pytest.mark.parametrize("key", ["repo", "base_branch", "mode", "test_command"])
def test_every_legacy_scalar_is_caught(tmp_path, key):
    from maajun.config import ConfigError

    path = tmp_path / "config.toml"
    path.write_text(f'[github]\n{key} = "x"\n')
    with pytest.raises(ConfigError):
        Config.load(path)


def test_the_new_format_loads_fine_alongside_an_empty_github_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[github]\n\n[[github.repos]]\nrepo = "acme/api"\n')
    assert [rc.repo for rc in Config.load(path).github.repos] == ["acme/api"]


def test_saving_strips_legacy_keys_so_the_next_load_succeeds(tmp_path):
    """save() must not leave keys behind that its own loader would reject."""
    path = tmp_path / "config.toml"
    path.write_text('[github]\nrepo = "acme/api"\nmode = "fix"\n')

    config = Config()
    config.add_repo(RepoConfig(repo="acme/api", mode="fix"))
    config.save(path)

    assert "[[github.repos]]" in path.read_text()
    assert [rc.repo for rc in Config.load(path).github.repos] == ["acme/api"]
