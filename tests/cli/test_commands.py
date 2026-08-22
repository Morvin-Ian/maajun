import keyring
import keyring.errors
import pytest
from typer.testing import CliRunner

from maajun.auth import AuthManager
from maajun.cli import app
from maajun.config import Config
from maajun.discovery import Discovered

runner = CliRunner()


def flat(text: str) -> str:
    """CLI output with runs of whitespace collapsed to single spaces.

    Rich wraps at the console width, so a message that interpolates a path can
    break between any two words once that path is long enough — "Delete it"
    arrives as "Delete\nit". CI's tmp_path is longer than a local one, so
    asserting on the raw text passes here and fails there. Flatten first and
    the assertion stops depending on where the wrap lands.
    """
    return " ".join(text.split())


@pytest.fixture
def fake_keyring(monkeypatch):
    """In-memory keyring for CLI tests."""
    store = {}

    def delete_password(service, name):
        if (service, name) not in store:
            raise keyring.errors.PasswordDeleteError(name)
        del store[(service, name)]

    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get((s, n)))
    monkeypatch.setattr(keyring, "set_password", lambda s, n, v: store.__setitem__((s, n), v))
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return store


class FakeGitHubClient:
    def __init__(self, token, **kwargs):
        self.token = token

    async def validate_token(self):
        return "morvin"

    async def can_push(self, repo):
        return True

    async def aclose(self):
        pass


# ---------------------------------------------------------------------------
# Main callback (no subcommand)
# ---------------------------------------------------------------------------


def test_main_shows_welcome_when_no_providers(fake_keyring):
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Welcome to Maajun" in result.output or "Setup Required" in result.output


def test_main_shows_providers_when_configured(fake_keyring, monkeypatch):
    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")

    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Configured" in result.output


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# provider-list
# ---------------------------------------------------------------------------


def test_provider_list(fake_keyring):
    result = runner.invoke(app, ["provider-list"])
    assert result.exit_code == 0
    assert "deepseek" in result.output.lower()


# ---------------------------------------------------------------------------
# config-set-key
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# config-remove-key
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# sign-out
# ---------------------------------------------------------------------------


def test_sign_out(fake_keyring):
    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")

    result = runner.invoke(app, ["sign-out"])
    assert result.exit_code == 0
    assert "cleared" in result.output.lower()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def test_watch_fails_without_api_key(fake_keyring, tmp_path):
    """The API key is the one hard requirement."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[github.repos]]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "No API key" in result.output


def test_watch_fails_when_a_repo_is_set_but_no_token(fake_keyring, tmp_path, monkeypatch):
    AuthManager().set_api_key("deepseek", "sk-test")
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[github.repos]]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "no GitHub token" in result.output


def test_watch_without_a_repo_runs_in_local_mode(fake_keyring, tmp_path, monkeypatch):
    """GitHub is optional: with no repo, errors are analyzed into local reports."""
    AuthManager().set_api_key("deepseek", "sk-test")
    log_file = tmp_path / "app.log"
    log_file.write_text("")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[monitor]\nlog_files = ["{log_file}"]\n'
        f'[daemon]\nworkdir = "{tmp_path / "data"}"\nrepo_path = "{tmp_path}"\n'
    )
    result = runner.invoke(app, ["watch", "--once", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "local" in result.output
    assert str(tmp_path) in result.output


# ---------------------------------------------------------------------------
# chat (non-interactive, provider not configured)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


def test_root_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Maajun" in result.output


def test_watch_help():
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output
    assert "--dry-run" in result.output
    assert "--verbose" in result.output


# ---------------------------------------------------------------------------
# watch --dry-run
# ---------------------------------------------------------------------------


def test_watch_dry_run_still_requires_an_api_key(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[github.repos]]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--dry-run", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "No API key" in result.output


# ---------------------------------------------------------------------------
# config / add-repo / status commands
# ---------------------------------------------------------------------------


def test_config_set_persists_and_validates(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[github.repos]]\nrepo = "owner/name"\nmode = "suggest"\n')

    result = runner.invoke(app, ["config", "github.mode", "fix", "--config", str(config_path)])
    assert result.exit_code == 0
    assert 'mode = "fix"' in config_path.read_text()

    bad = runner.invoke(app, ["config", "github.mode", "yolo", "--config", str(config_path)])
    assert bad.exit_code == 1
    assert 'mode = "fix"' in config_path.read_text()  # unchanged


def test_status_reports_missing_credentials(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[monitor]\nlog_files = []\n[[github.repos]]\nrepo = "owner/name"\n')

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 1  # missing API key + token
    assert "maajun setup" in result.output
    assert "maajun setup" in result.output


def test_status_all_green(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("maajun.cli.monitor.GitHubClient", FakeGitHubClient)
    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")
    auth.set_github_token("ghp_test")

    logf = tmp_path / "app.log"
    logf.write_text("")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[ai]\nprovider = "deepseek"\n'
        f'[[github.repos]]\nrepo = "owner/name"\n'
        f'[monitor]\nlog_files = ["{logf}"]\n'
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "Ready" in result.output


def test_add_repo_appends_to_the_list(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[github.repos]]\nrepo = "owner/first"\nmode = "suggest"\n')

    r1 = runner.invoke(
        app, ["add-repo", "owner/second", "-m", "fix", "--config", str(config_path)]
    )
    assert r1.exit_code == 0

    from maajun.config import Config

    cfg = Config.load(config_path)
    assert [rc.repo for rc in cfg.github.repos] == ["owner/first", "owner/second"]

    bad = runner.invoke(app, ["add-repo", "not-a-repo", "--config", str(config_path)])
    assert bad.exit_code == 1


def test_reset_removes_everything(fake_keyring, tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    auth = AuthManager()
    auth.set_api_key("deepseek", "sk-test")
    auth.set_github_token("ghp_test")

    (cfg_home / "maajun").mkdir(parents=True)
    (cfg_home / "maajun" / "config.toml").write_text('[[github.repos]]\nrepo = "owner/name"\n')
    (data_home / "maajun").mkdir(parents=True)
    (data_home / "maajun" / "incidents.db").write_text("")

    result = runner.invoke(app, ["reset", "--force"])
    assert result.exit_code == 0
    assert not (cfg_home / "maajun").exists()
    assert not (data_home / "maajun").exists()
    # A fresh manager reflects the cleared keyring (the old instance caches).
    fresh = AuthManager()
    assert not fresh.has_api_key("deepseek")
    assert not fresh.has_github_token()


def test_reset_cancels_without_confirmation(fake_keyring, tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (cfg_home / "maajun").mkdir(parents=True)
    (cfg_home / "maajun" / "config.toml").write_text("")

    result = runner.invoke(app, ["reset"], input="no\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    assert (cfg_home / "maajun").exists()  # nothing deleted


def test_an_old_format_config_is_a_clean_error_not_a_traceback(tmp_path):
    """The [github] in the message must survive Rich, which reads it as a tag."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[github]\nrepo = "owner/name"\nmode = "fix"\n')

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "[github]" in flat(result.output)
    assert "old single-repo format" in flat(result.output)
    assert "maajun add-repo owner/name" in flat(result.output)


def test_malformed_toml_is_a_clean_error_not_a_traceback(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[github\n")

    result = runner.invoke(app, ["incidents", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Could not read the config" in flat(result.output)


def test_incidents_migrates_an_outdated_database_and_still_lists_it(tmp_path):
    """An old incidents.db used to abort the command; now it is upgraded."""
    import sqlite3

    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "incidents.db")
    conn.execute(
        "CREATE TABLE incidents (fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL,"
        " message TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
        " count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',"
        " branch TEXT, pr_url TEXT, cost_usd REAL DEFAULT 0,"
        " prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO incidents (fingerprint, source, message, first_seen,"
        " last_seen, status, pr_url) VALUES ('deadbeef', 'logfile:/x.log',"
        " 'KeyError: discount', 't0', 't1', 'processed',"
        " 'https://github.com/a/b/pull/7')"
    )
    conn.commit()
    conn.close()

    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[daemon]\nworkdir = "{data}"\n')

    result = runner.invoke(app, ["incidents", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "Traceback" not in result.output
    assert "deadbeef" in flat(result.output)


def test_incidents_refuses_a_database_from_a_newer_maajun(tmp_path):
    import sqlite3

    from maajun.daemon.store import SCHEMA_VERSION, IncidentStore

    data = tmp_path / "data"
    data.mkdir()
    IncidentStore(data / "incidents.db").close()
    conn = sqlite3.connect(data / "incidents.db")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()

    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[daemon]\nworkdir = "{data}"\n')

    result = runner.invoke(app, ["incidents", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "newer version of maajun" in flat(result.output)


def test_re_adding_a_repo_only_changes_what_you_pass(tmp_path):
    """Regression: re-running add-repo to change the mode silently reverted the
    base branch to "main" and dropped the test command."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[github.repos]]\nrepo = "acme/api"\nbase_branch = "develop"\n'
        'mode = "fix"\ntest_command = "pytest -q"\n'
        'log_files = ["/var/log/api.log"]\n'
    )

    result = runner.invoke(
        app, ["add-repo", "acme/api", "-m", "suggest", "--config", str(config_path)]
    )
    assert result.exit_code == 0
    assert "Updated acme/api" in result.output

    from maajun.config import Config

    entry = Config.load(config_path).github.repos[0]
    assert entry.mode == "suggest"          # the one thing we asked to change
    assert entry.base_branch == "develop"   # untouched
    assert entry.test_command == "pytest -q"
    assert entry.log_files == ["/var/log/api.log"]


def test_a_new_repo_still_gets_the_documented_defaults(tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "Added acme/api" in result.output

    from maajun.config import Config

    entry = Config.load(config_path).github.repos[0]
    assert (entry.base_branch, entry.mode, entry.log_files) == ("main", "suggest", [])


def test_mode_override_warns_when_there_is_no_repo_to_apply_it_to(tmp_path):
    """Regression: -m fix was silently ignored in local mode.

    The override loops over [[github.repos]], which local mode has none of,
    so the flag looked accepted and changed nothing.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[ai]\nprovider = "deepseek"\n'
        f'[monitor]\nlog_files = ["{tmp_path / "app.log"}"]\n'
        f'[daemon]\nworkdir = "{tmp_path / "data"}"\n'
    )
    (tmp_path / "app.log").write_text("")

    result = runner.invoke(
        app, ["watch", "-c", str(config_path), "--once", "-m", "fix"]
    )
    assert "--mode fix has no effect" in flat(result.output)
    assert "maajun add-repo" in flat(result.output)


def test_mode_override_is_quiet_when_a_repo_is_configured(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[ai]\nprovider = "deepseek"\n'
        '[[github.repos]]\nrepo = "acme/api"\n'
        f'[monitor]\nlog_files = ["{tmp_path / "app.log"}"]\n'
        f'[daemon]\nworkdir = "{tmp_path / "data"}"\n'
    )
    (tmp_path / "app.log").write_text("")

    result = runner.invoke(
        app, ["watch", "-c", str(config_path), "--once", "-m", "fix"]
    )
    assert "has no effect" not in flat(result.output)


# ---------------------------------------------------------------------------
# reset refuses an unsafe workdir
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Point every directory reset() touches inside tmp_path.

    Config.load() resolves default_config_path from maajun.config, while
    settings.py holds its own imported binding — both need patching, or the
    workdir is never read and the guard never runs.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr("maajun.config.default_config_path", lambda: config_path)
    monkeypatch.setattr(
        "maajun.cli.settings.default_config_path", lambda: config_path
    )
    monkeypatch.setattr("maajun.cli.settings.default_data_dir", lambda: data_dir)
    return config_path, data_dir


def test_reset_refuses_to_delete_a_git_checkout(fake_keyring, tmp_path, isolated_dirs):
    """daemon.workdir is hand-edited TOML, and reset rmtree's it."""
    config_path, _ = isolated_dirs
    checkout = tmp_path / "myapp"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "important.py").write_text("keep me")
    config_path.write_text(f'[daemon]\nworkdir = "{checkout}"\n')

    result = runner.invoke(app, ["reset", "--force"])
    assert result.exit_code == 0
    assert "Not deleting daemon.workdir" in flat(result.output)
    assert "git checkout" in flat(result.output)
    assert (checkout / "important.py").exists()


def test_reset_still_removes_an_ordinary_workdir(fake_keyring, tmp_path, isolated_dirs):
    config_path, _ = isolated_dirs
    workdir = tmp_path / "maajun-data"
    workdir.mkdir()
    (workdir / "incidents.db").write_text("")
    config_path.write_text(f'[daemon]\nworkdir = "{workdir}"\n')

    result = runner.invoke(app, ["reset", "--force"])
    assert result.exit_code == 0
    assert not workdir.exists()


def test_unsafe_workdirs_are_refused():
    from pathlib import Path as P

    from maajun.cli.settings import unsafe_to_delete

    assert unsafe_to_delete(P(P.home().anchor))
    assert unsafe_to_delete(P.home())
    assert unsafe_to_delete(P.home().parent)


def test_a_plain_data_directory_is_allowed(tmp_path):
    from maajun.cli.settings import unsafe_to_delete

    assert unsafe_to_delete(tmp_path / "maajun-data") == ""


# ---------------------------------------------------------------------------
# Deployment: add-repo flags and `discover`
# ---------------------------------------------------------------------------


def repo_config(config_path):
    return Config.load(config_path).github.repos[0]


def test_add_repo_records_a_deployment(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--path", "/srv/api", "--port", "8000", "--runs", "docker compose",
        "--journald-units", "api.service, nginx.service",
        "--docker-containers", "api-web-1",
    ])

    assert result.exit_code == 0
    deployment = repo_config(config_path).deployment
    assert deployment.path == "/srv/api"
    assert deployment.port == 8000
    assert deployment.runs == "docker compose"
    assert deployment.journald_units == ["api.service", "nginx.service"]
    assert deployment.docker_containers == ["api-web-1"]


def test_re_adding_a_repo_leaves_its_deployment_alone(fake_keyring, tmp_path):
    """The "None means leave as is" contract, extended to deployment flags."""
    config_path = tmp_path / "config.toml"
    runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--path", "/srv/api", "--port", "8000",
    ])

    runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path), "--mode", "fix",
    ])

    entry = repo_config(config_path)
    assert entry.mode == "fix"
    assert (entry.deployment.path, entry.deployment.port) == ("/srv/api", 8000)


def test_a_port_outside_the_valid_range_is_rejected(fake_keyring, tmp_path):
    result = runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(tmp_path / "config.toml"),
        "--port", "99999",
    ])
    assert result.exit_code != 0


def test_config_sets_a_deployment_value_for_one_repo(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    result = runner.invoke(app, [
        "config", "github.deployment.port", "8000",
        "--repo", "acme/api", "--config", str(config_path),
    ])

    assert result.exit_code == 0
    assert repo_config(config_path).deployment.port == 8000


def test_a_deployment_value_needs_a_repo(fake_keyring, tmp_path):
    """A port belongs to one deployment; cascading it is never meant."""
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    result = runner.invoke(app, [
        "config", "github.deployment.port", "8000", "--config", str(config_path),
    ])

    assert result.exit_code == 1
    assert "needs a repository" in flat(result.output)


def test_discover_prints_what_it_finds_without_writing(
    fake_keyring, tmp_path, monkeypatch
):
    """Read-only by default: it reports, you decide."""
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])
    monkeypatch.setattr(
        "maajun.cli.deployment.discover",
        lambda repo, existing=None: Discovered(
            path="/srv/api", port=8000, runs="docker compose",
            docker_containers=["api-web-1"], notes=["container api-web-1"],
        ),
    )

    result = runner.invoke(app, ["discover", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "api-web-1" in flat(result.output)
    assert repo_config(config_path).deployment.docker_containers == []


def test_discover_save_writes_the_deployment(fake_keyring, tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])
    monkeypatch.setattr(
        "maajun.cli.deployment.discover",
        lambda repo, existing=None: Discovered(
            path="/srv/api", journald_units=["api.service"]
        ),
    )

    result = runner.invoke(app, [
        "discover", "--save", "--config", str(config_path),
    ])

    assert result.exit_code == 0
    deployment = repo_config(config_path).deployment
    assert deployment.path == "/srv/api"
    assert deployment.journald_units == ["api.service"]


def test_discover_says_when_it_finds_no_runtime_source(
    fake_keyring, tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )

    result = runner.invoke(app, ["discover", "--config", str(config_path)])

    assert "No runtime error source found" in flat(result.output)


def test_discover_without_a_repo_configured_says_so(fake_keyring, tmp_path):
    result = runner.invoke(app, ["discover", "--config", str(tmp_path / "c.toml")])

    assert result.exit_code == 1
    assert "No repositories configured" in flat(result.output)


def test_discover_rejects_an_unconfigured_repo(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    result = runner.invoke(app, [
        "discover", "--repo", "acme/nope", "--config", str(config_path),
    ])

    assert result.exit_code == 1
    assert "is not configured" in flat(result.output)


def test_discover_path_needs_one_repo(fake_keyring, tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    for repo in ("acme/api", "acme/web"):
        runner.invoke(app, ["add-repo", repo, "--config", str(config_path)])

    result = runner.invoke(app, [
        "discover", "--path", "/srv/api", "--config", str(config_path),
    ])

    assert result.exit_code == 1
    assert "pass --repo too" in flat(result.output)
