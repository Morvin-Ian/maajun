import pytest
from typer.testing import CliRunner

from maajun.auth import AuthManager
from maajun.cli import app
from maajun.config import Config, RepoConfig
from maajun.discovery.deployment import Discovered

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_probing(monkeypatch):
    """login runs discovery when it finishes; keep it off the real host."""
    monkeypatch.setattr(
        "maajun.cli.deployment.discover", lambda repo, existing=None: Discovered()
    )

    async def no_inspection(folder, ai):
        raise AssertionError("a test reached the real code inspection")

    monkeypatch.setattr("maajun.cli.deployment.inspect_repo", no_inspection)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
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


@pytest.fixture
def gh_ready(monkeypatch):
    """A machine with the GitHub CLI installed and a login available."""
    monkeypatch.setattr("maajun.cli.github_auth.gh_available", lambda: True)
    monkeypatch.setattr("maajun.cli.github_auth.gh_account", lambda: "tester")
    monkeypatch.setattr("maajun.cli.github_auth.gh_login", lambda: 0)
    monkeypatch.setattr("maajun.cli.github_auth.gh_token", lambda: "gho_new")
    monkeypatch.setattr("maajun.auth.gh_token", lambda: "gho_new")


def config_with_repo(path):
    config = Config()
    config.add_repo(RepoConfig(repo="acme/api"))
    config.save(path)


def test_choosing_the_github_cli_runs_its_login(fake_keyring, gh_ready, tmp_path):
    """The point of the command: no one should have to know the gh incantation."""
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, ["login", "-c", str(config_path)], input="1\n")

    assert result.exit_code == 0
    assert "How should maajun reach GitHub?" in result.output
    assert "Logged in with the GitHub CLI" in result.output
    # Borrowed, not copied: nothing of ours is stored.
    assert fake_keyring == {}


def test_choosing_a_token_stores_it(fake_keyring, tmp_path):
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app, ["login", "-c", str(config_path)], input="2\nghp_pasted\n"
    )

    assert result.exit_code == 0
    assert "Token stored" in result.output
    assert AuthManager().get_github_token() == "ghp_pasted"


def test_choosing_ssh_records_the_transport(fake_keyring, monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("maajun.cli.github_auth.ssh_works", lambda: True)

    result = runner.invoke(
        app, ["login", "-c", str(config_path)], input="3\n2\nghp_pasted\n"
    )

    assert result.exit_code == 0
    assert "pushed over SSH" in result.output
    assert Config.load(config_path).github.transport == "ssh"


def test_ssh_alone_still_asks_for_an_api_credential(fake_keyring, monkeypatch, tmp_path):
    """SSH pushes branches; issues and PRs go through the API."""
    monkeypatch.setattr("maajun.cli.github_auth.ssh_works", lambda: True)

    result = runner.invoke(
        app, ["login", "-c", str(tmp_path / "config.toml")],
        input="3\n2\nghp_pasted\n",
    )

    assert "issues and pull requests go" in result.output
    assert AuthManager().get_github_token() == "ghp_pasted"


def test_ssh_that_does_not_work_says_so(fake_keyring, monkeypatch, tmp_path):
    monkeypatch.setattr("maajun.cli.github_auth.ssh_works", lambda: False)

    result = runner.invoke(
        app, ["login", "-c", str(tmp_path / "config.toml")],
        input="3\n2\nghp_pasted\n",
    )

    assert "did not accept an SSH key" in result.output
    assert Config.load(tmp_path / "config.toml").github.transport == "auto"


def test_the_current_credential_is_shown_first(fake_keyring, tmp_path):
    AuthManager().set_github_token("ghp_existing")

    result = runner.invoke(
        app, ["login", "-c", str(tmp_path / "config.toml")],
        input="2\nghp_replacement\n",
    )

    assert "Token stored in the keyring" in result.output
    assert AuthManager().get_github_token() == "ghp_replacement"


def test_giving_up_exits_non_zero(fake_keyring, tmp_path):
    result = runner.invoke(
        app, ["login", "-c", str(tmp_path / "config.toml")], input="2\n\n"
    )

    assert result.exit_code == 1
    assert "No credential set" in result.output


def test_login_then_works_out_where_errors_land(fake_keyring, gh_ready, tmp_path):
    """Access is not enough: a repo maajun can push to but cannot read errors
    from is still a repo nobody is watching."""
    config_path = tmp_path / "config.toml"
    config_with_repo(config_path)

    result = runner.invoke(app, ["login", "-c", str(config_path)], input="1\n")

    assert "Where each repo's errors land" in result.output
    assert "maajun status" in result.output


def test_with_no_repo_it_says_what_to_do_next(fake_keyring, gh_ready, tmp_path):
    result = runner.invoke(
        app, ["login", "-c", str(tmp_path / "config.toml")], input="1\n"
    )

    assert "No repository configured yet" in result.output
    assert "add-repo" in result.output


def test_a_missing_gh_shows_how_to_install_it(fake_keyring, monkeypatch, tmp_path):
    monkeypatch.setattr("maajun.cli.github_auth.gh_available", lambda: False)

    result = runner.invoke(
        app, ["login", "-c", str(tmp_path / "config.toml")], input="1\n"
    )

    assert "not installed" in result.output
    assert "cli.github.com" in result.output
