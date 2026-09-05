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


@pytest.fixture(autouse=True)
def no_probing(monkeypatch):
    """add-repo probes the host for deployments; tests that care opt back in.

    Without this, every test that adds a repo would probe the developer's real
    machine, pick up whatever containers happen to be running, and take seconds
    doing it. Mirrors the fixture of the same name in test_setup.py.
    """
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )

    async def no_inspection(folder, ai):
        raise AssertionError("a test reached the real code inspection")

    monkeypatch.setattr("maajun.cli.deployment.inspect_repo", no_inspection)


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


def test_watch_fails_when_a_repo_is_set_but_no_token(
    fake_keyring, tmp_path, monkeypatch, default_provider
):
    AuthManager().set_api_key(default_provider, "sk-test")
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[github.repos]]\nrepo = "owner/name"\n')
    result = runner.invoke(app, ["watch", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "no GitHub token" in result.output


def test_watch_without_a_repo_runs_in_local_mode(
    fake_keyring, tmp_path, monkeypatch, default_provider
):
    """GitHub is optional: with no repo, errors are analyzed into local reports."""
    AuthManager().set_api_key(default_provider, "sk-test")
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
    monkeypatch.setattr("maajun.cli.watch.GitHubClient", FakeGitHubClient)
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


def test_reset_removes_everything(
    fake_keyring, tmp_path, monkeypatch, default_provider
):
    cfg_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    auth = AuthManager()
    auth.set_api_key(default_provider, "sk-test")
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


def test_add_repo_records_what_it_finds_running(fake_keyring, tmp_path, monkeypatch):
    """The point of the change: add-repo runs setup's discovery step.

    Before, a repo added this way had an empty deployment block, so the daemon
    could only see what GitHub showed it — no containers, no port, no logs.
    """
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("maajun.cli.deployment.discover", lambda repo, existing=None: Discovered(
        path="/srv/api", port=8000, runs="docker compose",
        docker_containers=["api-web-1"], notes=["container api-web-1"],
    ))

    result = runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    assert result.exit_code == 0
    deployment = repo_config(config_path).deployment
    assert (deployment.path, deployment.port) == ("/srv/api", 8000)
    assert deployment.runs == "docker compose"
    assert deployment.docker_containers == ["api-web-1"]
    assert "container api-web-1" in flat(result.output)


def test_discovery_does_not_overwrite_a_flag(fake_keyring, tmp_path, monkeypatch):
    """A path typed by hand outranks a guess from the host."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("maajun.cli.deployment.discover", lambda repo, existing=None: Discovered(
        path="/wrong/guess", port=9999,
    ))

    runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--path", "/srv/api", "--port", "8000",
    ])

    deployment = repo_config(config_path).deployment
    assert (deployment.path, deployment.port) == ("/srv/api", 8000)


def test_add_repo_does_not_prompt_without_a_terminal(fake_keyring, tmp_path):
    """Scripts and the chat tool call add-repo with nobody at the keyboard.

    A prompt there would hang on a stdin no one is typing into, so the flow is
    gated on an actual TTY rather than on the flag alone.
    """
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Base branch" not in result.output
    assert "Mode (1/2)" not in result.output
    entry = repo_config(config_path)
    assert (entry.base_branch, entry.mode) == ("main", "suggest")


def test_add_repo_asks_the_way_setup_does(fake_keyring, tmp_path, monkeypatch):
    """With a terminal, the same questions setup asks for its first repo."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("maajun.cli.watch.at_a_terminal", lambda: True)
    answers = iter(["develop", "2", "make test", ""])
    monkeypatch.setattr(
        "maajun.cli.shared.prompt_line", lambda text: next(answers)
    )

    result = runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    assert result.exit_code == 0
    entry = repo_config(config_path)
    assert entry.base_branch == "develop"
    assert entry.mode == "fix"
    assert entry.test_command == "make test"


def test_fix_mode_without_a_test_command_is_called_out(fake_keyring, tmp_path):
    """Fix mode edits code; an unverified PR should say so up front."""
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path), "--mode", "fix",
    ])

    assert "No test command" in flat(result.output)


def test_add_repo_records_a_test_command(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"

    runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--mode", "fix", "--test-command", "pytest -q",
    ])

    assert repo_config(config_path).test_command == "pytest -q"


def test_add_repo_records_separate_verification_commands(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--mode", "fix",
        "--verify-command", "ruff check .",
        "--verify-command", "mypy src",
        "--reproduction-command", "pytest -q tests/test_bug.py",
    ])

    assert result.exit_code == 0, result.output
    entry = repo_config(config_path)
    assert entry.verification_commands == ["ruff check .", "mypy src"]
    assert entry.reproduction_command == "pytest -q tests/test_bug.py"


def test_add_repo_accepts_automatic_mode_with_its_evidence(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--mode", "automatic",
        "--path", "/srv/api",
        "--verify-command", "pytest -q",
        "--reproduction-command", "pytest -q tests/test_bug.py",
    ])

    assert result.exit_code == 0, result.output
    entry = repo_config(config_path)
    assert entry.mode == "automatic"
    assert entry.deployment.path == "/srv/api"
    assert entry.verification_commands == ["pytest -q"]
    assert entry.reproduction_command == "pytest -q tests/test_bug.py"


def test_automatic_mode_without_evidence_is_called_out(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--mode", "automatic",
    ])

    assert result.exit_code == 0
    assert "Automatic mode will remain read-only" in flat(result.output)


def test_readding_a_repo_leaves_verification_untouched_when_flags_are_omitted(
    fake_keyring, tmp_path
):
    config_path = tmp_path / "config.toml"
    runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path),
        "--verify-command", "ruff check .",
        "--reproduction-command", "pytest -q tests/test_bug.py",
    ])

    runner.invoke(app, [
        "add-repo", "acme/api", "--config", str(config_path), "--mode", "fix",
    ])

    entry = repo_config(config_path)
    assert entry.verification_commands == ["ruff check ."]
    assert entry.reproduction_command == "pytest -q tests/test_bug.py"


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


# ---------------------------------------------------------------------------
# A repo name without an owner
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_in(monkeypatch):
    """GitHub authenticated, so the account name is known."""
    monkeypatch.setattr(
        "maajun.cli.watch.account_login", lambda token=None: "morvin"
    )


def test_add_repo_fills_in_the_owner(fake_keyring, signed_in, tmp_path):
    """Once the account is known, "myapp" is not ambiguous."""
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, ["add-repo", "myapp", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Using morvin/myapp" in flat(result.output)
    assert Config.load(config_path).github.repos[0].repo == "morvin/myapp"


def test_a_full_slug_is_left_alone(fake_keyring, signed_in, tmp_path):
    config_path = tmp_path / "config.toml"

    runner.invoke(app, ["add-repo", "acme/api", "--config", str(config_path)])

    assert Config.load(config_path).github.repos[0].repo == "acme/api"


def test_without_a_login_it_says_to_sign_in(fake_keyring, tmp_path):
    """Guessing an owner would put PRs on someone else's repo."""
    result = runner.invoke(
        app, ["add-repo", "myapp", "--config", str(tmp_path / "config.toml")]
    )

    assert result.exit_code == 1
    assert "maajun login" in flat(result.output)


def test_a_bare_name_still_reaches_the_repo_flags(fake_keyring, signed_in, tmp_path):
    config_path = tmp_path / "config.toml"

    runner.invoke(app, [
        "add-repo", "myapp", "--config", str(config_path),
        "-m", "fix", "--port", "8000",
    ])

    entry = Config.load(config_path).github.repos[0]
    assert (entry.repo, entry.mode, entry.deployment.port) == ("morvin/myapp", "fix", 8000)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def fake_background(watch_cli, monkeypatch, tmp_path) -> dict:
    """Stop watch actually detaching; report the arguments it would pass on."""
    launched: dict = {}

    def start(workdir, args):
        launched["args"] = args
        return watch_cli.service.Running(pid=1, log_file=tmp_path / "watch.log")

    async def no_daemon(config):
        return None

    monkeypatch.setattr(watch_cli.service, "running", lambda workdir: None)
    monkeypatch.setattr(watch_cli.service, "start", start)
    monkeypatch.setattr(watch_cli, "check_it_runs", no_daemon)
    return launched


def test_backfill_is_passed_to_the_background_daemon(fake_keyring, tmp_path, monkeypatch):
    """It is a per-run choice, and the run happens in another process."""
    from maajun.cli import watch as watch_cli

    launched = fake_background(watch_cli, monkeypatch, tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[ai]\nprovider = \"deepseek\"\n")

    result = runner.invoke(app, ["watch", "-c", str(config_path), "--backfill"])

    assert result.exit_code == 0
    assert "--backfill" in launched["args"]


def test_without_the_flag_the_daemon_is_not_told_to_backfill(
    fake_keyring, tmp_path, monkeypatch
):
    from maajun.cli import watch as watch_cli

    launched = fake_background(watch_cli, monkeypatch, tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[ai]\nprovider = \"deepseek\"\n")

    runner.invoke(app, ["watch", "-c", str(config_path)])

    assert "--backfill" not in launched["args"]
