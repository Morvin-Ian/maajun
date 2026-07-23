"""Tests for the status-check assembly (pure, no CliRunner)."""

from maajun.checks import build_status
from maajun.config import Config, GitHubConfig, MonitorConfig, RepoConfig


def _labels(sections):
    return [c.label for s in sections for c in s.checks]


def test_all_green(tmp_path):
    logf = tmp_path / "app.log"
    logf.write_text("")
    config = Config(
        github=GitHubConfig(repo="owner/name"),
        monitor=MonitorConfig(log_files=[str(logf)]),
    )
    repos = config.github.get_all_repos()

    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=repos, network=("morvin", {"owner/name": True}),
    )

    assert ok is True
    labels = _labels(sections)
    assert "API key for deepseek" in labels
    assert "Authenticated as morvin" in labels
    assert "Can push to owner/name" in labels


def test_missing_credentials_fail(tmp_path):
    config = Config(github=GitHubConfig(repo="owner/name"),
                    monitor=MonitorConfig(log_files=[str(tmp_path / "a.log")]))
    sections, ok = build_status(
        config, provider="deepseek", has_key=False, has_token=False,
        repos=config.github.get_all_repos(), network=None,
    )
    assert ok is False


def test_no_repo_configured_fails():
    config = Config(github=GitHubConfig(), monitor=MonitorConfig(log_files=["/x.log"]))
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[], network=None,
    )
    assert ok is False
    assert "Repository configured" in _labels(sections)


def test_missing_log_file_is_warning_not_failure(tmp_path):
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(log_files=[str(tmp_path / "missing.log")]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=config.github.get_all_repos(), network=("x", {"owner/name": True}),
    )
    # A missing log file is informational; overall status stays green.
    assert ok is True
    log_check = next(c for s in sections for c in s.checks if c.label.startswith("Log file"))
    assert log_check.ok is False
    assert log_check.warn is True
    assert log_check.counts is False


def test_reachability_not_checked_does_not_fail(tmp_path):
    logf = tmp_path / "app.log"
    logf.write_text("")
    config = Config(
        github=GitHubConfig(repo="owner/name"),
        monitor=MonitorConfig(log_files=[str(logf)]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=config.github.get_all_repos(), network=None,  # skipped probe
    )
    assert ok is True
    assert any("reachability not checked" in c.detail for s in sections for c in s.checks)


def test_cannot_push_fails(tmp_path):
    logf = tmp_path / "app.log"
    logf.write_text("")
    config = Config(
        github=GitHubConfig(repo="owner/name"),
        monitor=MonitorConfig(log_files=[str(logf)]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=config.github.get_all_repos(), network=("morvin", {"owner/name": False}),
    )
    assert ok is False
