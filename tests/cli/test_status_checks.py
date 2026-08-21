"""Tests for the status-check assembly (pure, no CliRunner)."""

from maajun.cli.status_checks import build_status
from maajun.config import Config, GitHubConfig, MonitorConfig, RepoConfig


def check_labels(sections):
    return [c.label for s in sections for c in s.checks]


def find(sections, label):
    return next(c for s in sections for c in s.checks if c.label == label)


def test_all_green(tmp_path):
    logf = tmp_path / "app.log"
    logf.write_text("")
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(log_files=[str(logf)]),
    )
    repos = config.github.get_all_repos()

    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=repos, network=("morvin", {"owner/name": True}),
    )

    assert ok is True
    labels = check_labels(sections)
    assert "API key for deepseek" in labels
    assert "Authenticated as morvin" in labels
    assert "Can push to owner/name" in labels


def test_missing_credentials_fail(tmp_path):
    config = Config(github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
                    monitor=MonitorConfig(log_files=[str(tmp_path / "a.log")]))
    sections, ok = build_status(
        config, provider="deepseek", has_key=False, has_token=False,
        repos=config.github.get_all_repos(), network=None,
    )
    assert ok is False


def test_no_repo_configured_is_a_warning_not_a_failure():
    """GitHub is optional — without a repo, reports are written to disk."""
    config = Config(github=GitHubConfig(), monitor=MonitorConfig(log_files=["/x.log"]))
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[], network=None,
    )
    assert ok is True
    assert "Repository configured" in check_labels(sections)


def test_missing_token_fails_once_a_repo_is_configured():
    """Asking for PRs without a token is a real misconfiguration."""
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(log_files=["/x.log"]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=False,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    assert ok is False
    assert "GitHub token stored" in check_labels(sections)


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
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
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
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(log_files=[str(logf)]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=config.github.get_all_repos(), network=("morvin", {"owner/name": False}),
    )
    assert ok is False


def test_no_monitors_at_all_still_fails():
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    assert ok is False
    assert "At least one monitor configured" in check_labels(sections)


# ---------------------------------------------------------------------------
# Log file readability
# ---------------------------------------------------------------------------


def status_for_log(tmp_path, log_path):
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(log_files=[str(log_path)]),
    )
    return build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )


def test_unreadable_log_file_fails_the_preflight(tmp_path):
    """The classic VPS misconfiguration: root-owned log, non-root daemon.

    exists() passed, status said ready, and the daemon then logged a
    PermissionError every single poll while detecting nothing.
    """
    log_file = tmp_path / "root-owned.log"
    log_file.write_text("")
    log_file.chmod(0o000)
    try:
        sections, ok = status_for_log(tmp_path, log_file)
        detail = next(c.detail for s in sections for c in s.checks
                      if c.label.startswith("Log file"))
        assert ok is False
        assert "not readable" in detail
    finally:
        log_file.chmod(0o644)


def test_readable_log_file_passes(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    _, ok = status_for_log(tmp_path, log_file)
    assert ok is True


def test_missing_log_file_is_still_only_a_warning(tmp_path):
    """It may not exist until the app logs its first error."""
    sections, ok = status_for_log(tmp_path, tmp_path / "not-yet.log")
    check = next(c for s in sections for c in s.checks if c.label.startswith("Log file"))
    assert ok is True
    assert check.warn is True


def test_token_check_says_stored_because_the_keyring_is_the_only_source():
    sections, _ = build_status(
        make_config(), provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    check = find(sections, "GitHub token stored")
    assert check.ok
    assert check.detail == ""


def test_missing_token_still_says_how_to_supply_one():
    sections, ok = build_status(
        make_config(), provider="deepseek", has_key=True, has_token=False,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    check = find(sections, "GitHub token stored")
    assert not check.ok and not ok
    assert "maajun setup" in check.detail
    # Neither the environment nor gh is a source any more.
    assert "GITHUB_TOKEN" not in check.detail
    assert "gh auth" not in check.detail


def make_config() -> Config:
    return Config(monitor=MonitorConfig(log_files=["/x.log"]))
