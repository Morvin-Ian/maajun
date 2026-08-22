import subprocess

import pytest
from typer.testing import CliRunner

from maajun.auth import AuthManager
from maajun.cli import app
from maajun.cli.setup import detect_repo_from_git
from maajun.config import Config, DeploymentConfig, RepoConfig
from maajun.discovery import Discovered
from maajun.inspection import Inspection

runner = CliRunner()


@pytest.fixture
def api_key(fake_keyring):
    """A key already in the keyring — the only place maajun reads one from."""
    AuthManager().set_api_key("deepseek", "sk-test")


@pytest.fixture
def no_git_detect(monkeypatch):
    """Stop the wizard picking up the repo maajun itself is developed in."""
    monkeypatch.setattr("maajun.cli.setup.detect_repo_from_git", lambda *a: None)


@pytest.fixture(autouse=True)
def no_probing(monkeypatch):
    """Setup probes the host for deployments; tests that care opt back in."""
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )
    async def no_inspection(folder, ai):
        raise AssertionError("a test reached the real code inspection")

    monkeypatch.setattr("maajun.cli.deployment.inspect_repo", no_inspection)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Never let the push-access probe reach the real GitHub API."""
    class Client:
        def __init__(self, token, **kwargs):
            self.token = token

        async def validate_token(self):
            return "tester"

        async def can_push(self, repo):
            return True

        async def aclose(self):
            pass

    monkeypatch.setattr("maajun.cli.github_auth.GitHubClient", Client)


# ---------------------------------------------------------------------------
# Repo autodetection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:owner/name.git", "owner/name"),
    ("https://github.com/owner/name", "owner/name"),
    ("https://github.com/owner/name.git", "owner/name"),
    ("https://github.com/owner/name/", "owner/name"),
    ("https://gitlab.com/owner/name.git", None),
    ("", None),
])
def test_detect_repo_parses_remote_forms(monkeypatch, url, expected):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=url, stderr="")

    monkeypatch.setattr("maajun.cli.setup.subprocess.run", run)
    assert detect_repo_from_git() == expected


def test_detect_repo_is_quiet_outside_a_checkout(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr("maajun.cli.setup.subprocess.run", run)
    assert detect_repo_from_git() is None


# ---------------------------------------------------------------------------
# Non-interactive setup
# ---------------------------------------------------------------------------


def test_setup_needs_only_an_api_key(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(
        app, ["setup", "--non-interactive", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    assert config_path.exists()


def test_setup_fails_without_an_api_key(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(
        app, ["setup", "--non-interactive", "--config", str(config_path)]
    )
    assert result.exit_code == 1
    assert "No API key" in result.output


def test_setup_never_writes_the_placeholder_repo(fake_keyring, api_key, tmp_path):
    """Regression: a written placeholder read back as a configured repo, so the
    daemon demanded a token to push to owner/name."""
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["setup", "--non-interactive", "--config", str(config_path)])

    assert 'repo = "owner/name"' not in config_path.read_text()
    assert Config.load(config_path).github.get_all_repos() == []


def test_setup_records_repo_logs_and_mode(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    log_file = tmp_path / "app.log"
    log_file.write_text("")

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "fix", "--base-branch", "develop",
        "--logs", str(log_file),
    ])
    assert result.exit_code == 0, result.output

    config = Config.load(config_path)
    repos = config.github.get_all_repos()
    assert repos[0].repo == "acme/webapp"
    assert repos[0].mode == "fix"
    assert repos[0].base_branch == "develop"
    assert config.monitor.log_files == [str(log_file)]


def test_setup_rejects_a_malformed_repo_without_aborting(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "not-a-repo",
    ])
    assert result.exit_code == 0, result.output
    assert "owner/name form" in result.output
    assert Config.load(config_path).github.get_all_repos() == []


def test_setup_rejects_an_unknown_provider(fake_keyring, api_key, tmp_path):
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--provider", "nonesuch",
    ])
    assert result.exit_code == 1
    assert "Unknown provider" in result.output


def test_setup_is_idempotent(fake_keyring, api_key, no_git_detect, tmp_path):
    """Re-running must not duplicate repos or lose settings."""
    config_path = tmp_path / "config.toml"
    args = [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "fix",
    ]
    runner.invoke(app, args)
    runner.invoke(app, args)

    config = Config.load(config_path)
    assert len(config.github.get_all_repos()) == 1
    assert config.github.get_all_repos()[0].mode == "fix"


def test_setup_preserves_a_second_repo(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/first",
    ])
    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/second",
    ])

    repos = [rc.repo for rc in Config.load(config_path).github.get_all_repos()]
    assert repos == ["acme/first", "acme/second"]


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------


def test_github_actions_never_writes_the_token_to_config(fake_keyring, api_key, tmp_path):
    """Regression: the wizard copied the keyring token into config.toml,
    downgrading a secret that was safely stored."""
    config_path = tmp_path / "config.toml"
    AuthManager().set_github_token("ghp_stored")

    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--github-actions",
    ])
    config = Config.load(config_path)
    assert config.monitor.github_actions_repos == ["acme/webapp"]
    assert "ghp_stored" not in config_path.read_text()


def test_github_actions_is_skipped_without_a_token(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--github-actions",
    ])
    assert "needs a GitHub token" in result.output
    assert Config.load(config_path).monitor.github_actions_repos == []


def test_actions_alone_warns_that_runtime_errors_go_unwatched(
    fake_keyring, api_key, tmp_path
):
    """Actions only reports CI. Failed requests need a log file."""
    config_path = tmp_path / "config.toml"
    AuthManager().set_github_token("ghp_stored")

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--github-actions",
    ])
    assert "Nothing watches runtime errors for acme/webapp" in result.output


def test_a_log_file_alongside_actions_is_kept_and_not_warned_about(
    fake_keyring, api_key, tmp_path
):
    """Regression guard: accepting Actions must not displace the log monitor."""
    config_path = tmp_path / "config.toml"
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    AuthManager().set_github_token("ghp_stored")

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--logs", str(log_file), "--github-actions",
    ])
    config = Config.load(config_path)
    assert config.monitor.log_files == [str(log_file)]
    assert config.monitor.github_actions_repos == ["acme/webapp"]
    assert "Nothing watches runtime errors" not in result.output


# ---------------------------------------------------------------------------
# Closing summary
# ---------------------------------------------------------------------------


def test_summary_points_at_local_reports_without_a_repo(
    fake_keyring, api_key, no_git_detect, tmp_path
):
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--logs", str(log_file),
    ])
    assert "local reports" in result.output


def test_summary_points_at_prs_once_a_repo_is_set(fake_keyring, api_key, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    AuthManager().set_github_token("ghp_stored")
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--repo", "acme/webapp", "--logs", str(log_file),
    ])
    assert "--dry-run" in result.output


# ---------------------------------------------------------------------------
# Fix-mode verification
# ---------------------------------------------------------------------------


def test_setup_records_a_test_command(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "fix", "--test-command", "pytest -q",
    ])
    assert result.exit_code == 0, result.output
    assert Config.load(config_path).github.get_all_repos()[0].test_command == "pytest -q"


def test_fix_mode_without_a_test_command_warns(fake_keyring, api_key, tmp_path):
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--repo", "acme/webapp", "--mode", "fix",
    ])
    assert "unverified" in result.output.lower()


def test_suggest_mode_is_not_asked_for_a_test_command(fake_keyring, api_key, tmp_path):
    """Suggest mode has no diff, so there is nothing to verify."""
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--repo", "acme/webapp", "--mode", "suggest",
    ])
    assert "unverified" not in result.output.lower()


# ---------------------------------------------------------------------------
# Deployment discovery
# ---------------------------------------------------------------------------


def test_setup_records_what_it_finds_running(fake_keyring, api_key, monkeypatch, tmp_path):
    """The answer to "where do this repo's errors land" comes from the host,
    not from the user's memory of a path."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("maajun.cli.deployment.discover", lambda repo, existing=None: Discovered(
        path="/srv/webapp", port=8000, runs="docker compose",
        docker_containers=["webapp-web-1"], notes=["container webapp-web-1"],
    ))

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])

    deployment = Config.load(config_path).github.repos[0].deployment
    assert deployment.docker_containers == ["webapp-web-1"]
    assert (deployment.path, deployment.port) == ("/srv/webapp", 8000)
    assert "container webapp-web-1" in result.output


def test_discovery_does_not_overwrite_a_path_already_configured(
    fake_keyring, api_key, monkeypatch, tmp_path
):
    """A probe is a guess; something typed by hand is not."""
    config_path = tmp_path / "config.toml"
    runner.invoke(app, [
        "add-repo", "acme/webapp", "--config", str(config_path),
        "--path", "/opt/mine",
    ])
    monkeypatch.setattr("maajun.cli.deployment.discover", lambda repo, existing=None: Discovered(
        path="/srv/guessed", docker_containers=["webapp-web-1"],
    ))

    runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])

    deployment = Config.load(config_path).github.repos[0].deployment
    assert deployment.path == "/opt/mine"
    assert deployment.docker_containers == ["webapp-web-1"]


def test_discovery_always_runs(fake_keyring, api_key, monkeypatch, tmp_path):
    """Not optional: a setup that never worked out where the errors land has
    not set anything up."""
    called = []
    monkeypatch.setattr(
        "maajun.cli.deployment.discover",
        lambda repo, existing=None: called.append(repo) or Discovered(),
    )

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--repo", "acme/webapp",
    ])

    assert result.exit_code == 0
    assert called == ["acme/webapp"]


def test_there_is_no_way_to_skip_discovery(fake_keyring, api_key, tmp_path):
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--no-discover",
    ])

    assert result.exit_code != 0


def test_a_repo_that_says_runtime_none_is_not_nagged(
    fake_keyring, api_key, tmp_path
):
    config_path = tmp_path / "config.toml"
    config = Config()
    config.add_repo(RepoConfig(
        repo="acme/webapp", deployment=DeploymentConfig(runtime="none"),
    ))
    config.save(config_path)

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])

    assert "Nothing watches runtime errors" not in result.output


def test_setup_records_what_reading_the_code_finds(
    fake_keyring, api_key, monkeypatch, tmp_path
):
    """The point of the AI pass: the log path comes from the code, not from
    the user remembering it."""
    config_path = tmp_path / "config.toml"
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(
        "maajun.cli.deployment.discover",
        lambda repo, existing=None: Discovered(path=str(app_dir)),
    )

    async def fake_inspection(folder, ai):
        return Inspection(
            stack="Django 5 + gunicorn",
            port=8000,
            log_files=[str(app_dir / "logs" / "error.log")],
            logging_gaps=["views.py:11 - except Exception: pass"],
            logging_advice="Create logs/ at startup",
        )

    monkeypatch.setattr("maajun.cli.deployment.inspect_repo", fake_inspection)

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])

    deployment = Config.load(config_path).github.repos[0].deployment
    assert deployment.stack == "Django 5 + gunicorn"
    assert deployment.port == 8000
    assert deployment.log_files == [str(app_dir / "logs" / "error.log")]
    assert "except Exception: pass" in result.output
    assert "Create logs/ at startup" in result.output


def test_a_failed_inspection_does_not_stop_setup(
    fake_keyring, api_key, monkeypatch, tmp_path
):
    """It is an optional extra; setup still has to finish."""
    config_path = tmp_path / "config.toml"
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(
        "maajun.cli.deployment.discover",
        lambda repo, existing=None: Discovered(path=str(app_dir)),
    )
    async def explode(folder, ai):
        raise RuntimeError("provider down")

    monkeypatch.setattr("maajun.cli.deployment.inspect_repo", explode)

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])

    assert result.exit_code == 0
    assert "Could not read the code" in result.output
    assert Config.load(config_path).github.repos[0].deployment.path == str(app_dir)


def test_setup_never_asks_for_a_path(fake_keyring, api_key, monkeypatch, tmp_path):
    """Discovery and the code are the source of truth; a path typed from
    memory is how a repo ends up watching a file nothing writes."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )

    result = runner.invoke(app, [
        "setup", "--config", str(config_path), "--repo", "acme/webapp",
    ], input="\n\n1\nn\nn\n")

    assert "Where do its errors land" not in result.output
    assert "path/unit/container" not in result.output


def test_a_repo_with_nothing_watching_it_is_told_how_to_fix_that(
    fake_keyring, api_key, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
        "--repo", "acme/webapp",
    ])

    assert "Nothing watches acme/webapp for runtime errors yet" in result.output
    assert "maajun discover -r acme/webapp --save" in flat(result.output)


def test_the_owner_is_filled_in_from_the_login(
    fake_keyring, api_key, monkeypatch, tmp_path
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(
        "maajun.cli.setup.account_login", lambda token=None: "morvin"
    )
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path), "--repo", "myapp",
    ])

    assert "Using morvin/myapp" in flat(result.output)
    assert Config.load(config_path).github.repos[0].repo == "morvin/myapp"


def flat(text):
    return " ".join(text.split())
