from maajun.cli.status_checks import build_status
from maajun.config import (
    Config,
    DeploymentConfig,
    GitHubConfig,
    MonitorConfig,
    RepoConfig,
)


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
    log_check = next(c for s in sections for c in s.checks if c.label.startswith("log file"))
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


def test_actions_alone_fails_the_preflight():
    """CI-only leaves every failed request unwatched, which is the whole
    point of the tool — so it blocks `watch` rather than warning."""
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(github_actions_repos=["owner/name"]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    assert ok is False
    assert find(sections, "runtime error source").detail.startswith("none configured")


def test_each_repo_is_reported_with_its_own_sources(tmp_path):
    """With several repos, a source line has to say which app it watches."""
    api = RepoConfig(repo="acme/api", deployment=DeploymentConfig(
        docker_containers=["api-web-1"]))
    web = RepoConfig(repo="acme/web", deployment=DeploymentConfig(
        journald_units=["web.service"]))
    config = Config(github=GitHubConfig(repos=[api, web]))

    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[api, web], network=None,
        probe=lambda kind, target: (True, "", False),
    )

    assert ok is True
    labels = check_labels(sections)
    assert "acme/api — docker: api-web-1" in labels
    assert "acme/web — journald: web.service" in labels


def test_a_global_log_file_counts_for_the_repo_it_attaches_to(tmp_path):
    """It feeds repo #1, so repo #1 is not sourceless — but repo #2 is."""
    logf = tmp_path / "app.log"
    logf.write_text("")
    api = RepoConfig(repo="acme/api")
    web = RepoConfig(repo="acme/web")
    config = Config(
        github=GitHubConfig(repos=[api, web]),
        monitor=MonitorConfig(log_files=[str(logf)]),
    )

    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[api, web], network=None,
    )

    assert ok is False
    labels = check_labels(sections)
    assert f"acme/api — log file {logf}" in labels
    assert "acme/web — runtime error source" in labels


def test_an_unreachable_source_fails_and_a_stopped_one_warns():
    """A typo'd container name is a mistake; a stopped one still has logs."""
    repo = RepoConfig(repo="acme/api", deployment=DeploymentConfig(
        docker_containers=["gone", "stopped"]))
    config = Config(github=GitHubConfig(repos=[repo]))

    def probe(kind, target):
        if target == "gone":
            return False, "no such container", False
        return False, "container is exited", True

    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[repo], network=None, probe=probe,
    )

    assert ok is False
    assert find(sections, "docker: gone").counts is True
    stopped = find(sections, "docker: stopped")
    assert stopped.warn and stopped.counts is False


def test_a_configured_folder_that_is_missing_is_only_a_warning(tmp_path):
    """The daemon can run somewhere the app is not deployed."""
    repo = RepoConfig(repo="acme/api", deployment=DeploymentConfig(
        path=str(tmp_path / "gone"), docker_containers=["api"]))
    config = Config(github=GitHubConfig(repos=[repo]))

    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[repo], network=None,
        probe=lambda kind, target: (True, "", False),
    )

    assert ok is True
    check = find(sections, f"folder {tmp_path / 'gone'}")
    assert check.warn and check.counts is False


def test_runtime_none_is_how_a_ci_only_repo_passes():
    """The explicit opt-out: said out loud in config, not inferred."""
    repo = RepoConfig(
        repo="owner/name", deployment=DeploymentConfig(runtime="none")
    )
    config = Config(
        github=GitHubConfig(repos=[repo]),
        monitor=MonitorConfig(github_actions_repos=["owner/name"]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[repo], network=None,
    )
    assert ok is True
    assert find(sections, "no runtime source").detail == 'runtime = "none"'


def test_actions_with_a_log_file_does_not_warn(tmp_path):
    logf = tmp_path / "app.log"
    logf.write_text("")
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name")]),
        monitor=MonitorConfig(
            log_files=[str(logf)], github_actions_repos=["owner/name"]
        ),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    assert ok is True
    assert "runtime error source" not in check_labels(sections)


def test_a_per_repo_log_file_counts_as_runtime_coverage(tmp_path):
    """The log path can live on the repo entry rather than [monitor]."""
    logf = tmp_path / "app.log"
    logf.write_text("")
    repo = RepoConfig(repo="owner/name", log_files=[str(logf)])
    config = Config(
        github=GitHubConfig(repos=[repo]),
        monitor=MonitorConfig(github_actions_repos=["owner/name"]),
    )
    sections, ok = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[repo], network=None,
    )
    assert ok is True
    assert "runtime error source" not in check_labels(sections)


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
                      if c.label.startswith("log file"))
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
    check = next(c for s in sections for c in s.checks if c.label.startswith("log file"))
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


def test_missing_token_names_both_ways_to_supply_one():
    sections, ok = build_status(
        make_config(), provider="deepseek", has_key=True, has_token=False,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )
    check = find(sections, "GitHub token stored")
    assert not check.ok and not ok
    assert "gh auth login" in check.detail
    assert "maajun setup" in check.detail
    # The environment is still not a source; only the keyring and gh are.
    assert "GITHUB_TOKEN" not in check.detail


def test_a_borrowed_gh_login_says_where_it_came_from():
    """Pushing as someone else's account is worth seeing before it happens."""
    sections, _ = build_status(
        make_config(), provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None, token_source="gh",
    )

    assert "GitHub credential from the gh CLI" in check_labels(sections)


def test_the_push_transport_is_shown():
    config = make_config()
    config.github.transport = "ssh"

    sections, _ = build_status(
        config, provider="deepseek", has_key=True, has_token=True,
        repos=[RepoConfig(repo="owner/name")], network=None,
    )

    assert "Pushing over SSH" in check_labels(sections)


def make_config() -> Config:
    return Config(monitor=MonitorConfig(log_files=["/x.log"]))
