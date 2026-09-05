import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maajun.auth import AuthManager
from maajun.cli import app
from maajun.cli.setup import (
    ask_verification_commands,
    catalog_line,
    checkout_candidates,
    detect_repo_from_git,
    model_line,
    pick_from_gateway,
    pick_provider,
    setup_model,
    split_by_kind,
)
from maajun.cli.shared import Asker, implemented_providers
from maajun.config import Config, DeploymentConfig, GitHubConfig, RepoConfig
from maajun.project.discovery import Discovered
from maajun.project.inspection import Inspection
from maajun.providers.catalog import CatalogEntry

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
    assert Config.load(config_path).github.repos == []


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
    repos = config.github.repos
    assert repos[0].repo == "acme/webapp"
    assert repos[0].mode == "fix"
    assert repos[0].base_branch == "develop"
    assert config.monitor.log_files == [str(log_file)]


def test_setup_accepts_automatic_mode(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    log_file = tmp_path / "app.log"
    log_file.write_text("")

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "automatic",
        "--test-command", "pytest -q",
        "--reproduction-command", "pytest -q tests/test_bug.py",
        "--logs", str(log_file),
    ])

    assert result.exit_code == 0, result.output
    assert Config.load(config_path).github.repos[0].mode == "automatic"


def test_setup_rejects_a_malformed_repo_without_aborting(fake_keyring, api_key, tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "not-a-repo",
    ])
    assert result.exit_code == 0, result.output
    assert "owner/name form" in result.output
    assert Config.load(config_path).github.repos == []


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
    assert len(config.github.repos) == 1
    assert config.github.repos[0].mode == "fix"


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

    repos = [rc.repo for rc in Config.load(config_path).github.repos]
    assert repos == ["acme/first", "acme/second"]


# ---------------------------------------------------------------------------
# Nothing watching the runtime
# ---------------------------------------------------------------------------


def test_a_repo_with_no_source_warns_that_runtime_errors_go_unwatched(
    fake_keyring, api_key, tmp_path
):
    """A repo nothing watches is a repo whose failed requests nobody sees."""
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])
    assert "Nothing watches runtime errors for acme/webapp" in result.output


def test_a_log_file_is_kept_and_silences_the_warning(
    fake_keyring, api_key, tmp_path
):
    config_path = tmp_path / "config.toml"
    log_file = tmp_path / "app.log"
    log_file.write_text("")

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--logs", str(log_file),
    ])
    config = Config.load(config_path)
    assert config.monitor.log_files == [str(log_file)]
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
    assert Config.load(config_path).github.repos[0].test_command == "pytest -q"


def test_setup_records_repeated_verification_and_reproduction_flags(
    fake_keyring, api_key, tmp_path
):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp", "--mode", "suggest",
        "--verify-command", "ruff check .",
        "--verify-command", "mypy src",
        "--reproduction-command", "pytest -q tests/test_bug.py",
    ])

    assert result.exit_code == 0, result.output
    entry = Config.load(config_path).github.repos[0]
    assert entry.mode == "suggest"
    assert entry.verification_commands == ["ruff check .", "mypy src"]
    assert entry.reproduction_command == "pytest -q tests/test_bug.py"


def test_setup_preserves_the_target_repos_verification_when_flags_are_omitted(
    fake_keyring, api_key, tmp_path
):
    config_path = tmp_path / "config.toml"
    config = Config(
        github=GitHubConfig(repos=[
            RepoConfig(
                repo="acme/first",
                mode="fix",
                verification_commands=["ruff check ."],
            ),
            RepoConfig(
                repo="acme/second",
                mode="suggest",
                verification_commands=["mypy src"],
                reproduction_command="pytest -q tests/test_second_bug.py",
            ),
        ])
    )
    config.save(config_path)

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/second",
    ])

    assert result.exit_code == 0, result.output
    second = Config.load(config_path).github.repos[1]
    assert second.mode == "suggest"
    assert second.verification_commands == ["mypy src"]
    assert second.reproduction_command == "pytest -q tests/test_second_bug.py"


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


# ---------------------------------------------------------------------------
# A machine with no keyring
# ---------------------------------------------------------------------------


@pytest.fixture
def headless(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("maajun.cli.setup.keyring_works", lambda: False)


def test_a_server_is_told_where_its_credentials_went(
    fake_keyring, headless, tmp_path
):
    """Said, not asked: there is one sensible answer on a machine with no
    keyring, and a question there is friction for nothing."""
    result = runner.invoke(app, [
        "setup", "--config", str(tmp_path / "config.toml"),
    ], input="\n\n")

    output = flat(result.output)
    assert "No keyring on this machine" in output
    # Rich wraps a long path, so assert on parts that cannot split.
    assert "chmod 600" in output
    assert "keyrings.alt" in output  # the alternative, for anyone who wants it
    assert "Choice" not in output


def test_the_notice_comes_before_the_key_is_typed(fake_keyring, headless, tmp_path):
    result = runner.invoke(app, [
        "setup", "--config", str(tmp_path / "config.toml"),
    ], input="\n\n")

    output = flat(result.output)
    assert output.index("No keyring") < output.index("API key (input hidden)")


def test_a_machine_with_a_keyring_hears_nothing_about_files(
    fake_keyring, monkeypatch, tmp_path
):
    monkeypatch.setattr("maajun.cli.setup.keyring_works", lambda: True)

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(tmp_path / "config.toml"),
    ])

    assert "credentials.json" not in flat(result.output)


def test_setup_finishes_unattended_without_a_keyring(
    fake_keyring, api_key, headless, tmp_path
):
    """--non-interactive used to be a dead end on exactly the machines maajun
    is deployed to."""
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, [
        "setup", "--non-interactive", "--config", str(config_path),
        "--repo", "acme/webapp",
    ])

    assert result.exit_code == 0
    assert config_path.exists()


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------


class Answer(Asker):
    """An Asker that gives one canned reply to every prompt."""

    def __init__(self, reply: str = ""):
        super().__init__(interactive=True)
        self.reply = reply
        self.prompts: list[str] = []

    def text(self, prompt: str, default: str = "") -> str:
        self.prompts.append(prompt)
        return self.reply or default


def test_setup_model_takes_a_number_from_the_catalog():
    config = Config()
    setup_model(Answer("3"), config, "anthropic", None)
    assert config.ai.model == "claude-opus-5"


def test_choosing_the_provider_default_leaves_the_model_unset():
    """Pinning it would freeze the cheap tier where the provider replaces it."""
    config = Config()
    setup_model(Answer("1"), config, "anthropic", None)
    assert config.ai.model is None


def test_setup_model_takes_an_id_that_is_not_in_the_catalog():
    config = Config()
    setup_model(Answer("claude-sonnet-5-20260101"), config, "anthropic", None)
    assert config.ai.model == "claude-sonnet-5-20260101"


def test_the_model_flag_wins_and_never_prompts():
    config = Config()
    ask = Answer("1")
    setup_model(ask, config, "openai", "gpt-4o")
    assert config.ai.model == "gpt-4o"
    assert ask.prompts == []


def test_non_interactive_setup_leaves_the_model_alone():
    config = Config()
    config.ai.model = "gpt-4o"
    setup_model(Asker(interactive=False), config, "openai", None)
    assert config.ai.model == "gpt-4o"


def test_a_gateway_asks_for_a_model_id_rather_than_a_list():
    config = Config()
    ask = Answer("anthropic/claude-opus-5")
    setup_model(ask, config, "openrouter", None)
    assert config.ai.model == "anthropic/claude-opus-5"
    assert "vendor/model" not in "".join(ask.prompts)  # that is said, not asked


def test_a_gateway_left_blank_sets_no_model():
    """It has no default to fall back to, so this has to stay unset."""
    config = Config()
    setup_model(Answer(""), config, "straitly", None)
    assert config.ai.model is None


def test_a_catalog_line_carries_the_price_and_the_role():
    from maajun.providers.anthropic import AnthropicProvider

    line = model_line(AnthropicProvider, AnthropicProvider.models[0])
    assert "claude-haiku-4-5" in line
    assert "$1.00 in / $5.00 out" in line
    assert "default" in line


def test_changing_provider_clears_a_model_chosen_for_the_old_one():
    """Sending one provider's model id to another only fails on the first
    real call, which is a long way from the command that caused it."""
    config = Config()
    config.ai.provider = "anthropic"
    config.ai.model = "claude-opus-5"
    config.set("ai.provider", "openai")
    assert config.ai.model is None


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_the_providers_are_listed_downwards_and_grouped(capsys):
    pick_provider(Answer("1"), implemented_providers(), "deepseek")
    lines = [
        line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()
    ]

    heads = [
        index for index, line in enumerate(lines)
        if line.startswith(("Vendors", "Gateways"))
    ]
    assert len(heads) == 2 and heads[0] < heads[1]
    # One provider per line, numbered, rather than all of them on one.
    assert "1. deepseek" in lines
    for name in implemented_providers():
        assert sum(name in line for line in lines) == 1


def test_a_number_reaches_a_gateway_listed_after_the_vendors():
    """The gateways carry on the vendors' numbering, so 4 is not 1 again."""
    names = implemented_providers()
    vendors, gateways = split_by_kind(names)
    assert pick_provider(Answer(str(len(vendors) + 1)), names, "") == gateways[0]


def test_a_provider_can_still_be_typed_by_name():
    assert pick_provider(Answer("anthropic"), implemented_providers(), "") == "anthropic"


def test_an_out_of_range_number_is_left_for_the_caller_to_reject():
    assert pick_provider(Answer("99"), implemented_providers(), "") == "99"


def test_nothing_is_listed_when_nobody_is_there_to_read_it(capsys):
    ask = Asker(interactive=False)
    assert pick_provider(ask, implemented_providers(), "openai") == "openai"
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# A gateway's fetched catalogue
# ---------------------------------------------------------------------------


class Replies(Asker):
    """An Asker that answers each prompt in turn."""

    def __init__(self, *replies: str):
        super().__init__(interactive=True)
        self.replies = list(replies)
        self.prompts: list[str] = []

    def text(self, prompt: str, default: str = "") -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else default


def entry(model_id, vendor, input_rate=1.0, output_rate=5.0):
    return CatalogEntry(id=model_id, vendor=vendor, input=input_rate, output=output_rate)


CATALOG = (
    entry("anthropic/claude-opus-5", "anthropic", 5.0, 25.0),
    entry("anthropic/claude-haiku-4.5", "anthropic", 1.0, 5.0),
    entry("openai/gpt-5.2", "openai", 1.75, 14.0),
)


def gateway():
    from maajun.cli.setup import provider_class

    return provider_class("openrouter")


def test_a_gateway_lists_vendors_first_then_that_vendors_models(capsys):
    """396 models do not fit in one list, and their own catalogues are
    grouped this way too."""
    ask = Replies("1", "2")

    # Sorted by id, so within anthropic the second is opus, not haiku.
    assert pick_from_gateway(ask, gateway(), CATALOG, None) == "anthropic/claude-opus-5"

    output = flat(capsys.readouterr().out)
    assert "3 models from 2 vendors" in output
    assert "1. anthropic (2)" in output
    assert "2. openai (1)" in output
    # The models of the vendor picked, and only those.
    assert "claude-haiku-4.5" in output
    assert "gpt-5.2" not in output


def test_an_id_typed_at_the_vendor_prompt_skips_the_second_step():
    ask = Replies("openai/gpt-5.4")
    assert pick_from_gateway(ask, gateway(), CATALOG, None) == "openai/gpt-5.4"
    assert len(ask.prompts) == 1


def test_an_id_typed_at_the_model_prompt_is_taken_as_it_is():
    ask = Replies("1", "anthropic/claude-opus-5:batch")
    chosen = pick_from_gateway(ask, gateway(), CATALOG, None)
    assert chosen == "anthropic/claude-opus-5:batch"


def test_answering_nothing_leaves_the_model_unset():
    assert pick_from_gateway(Replies("", ""), gateway(), CATALOG, None) is None


def test_setup_model_reads_the_catalogue_when_there_is_a_key(monkeypatch):
    config = Config()
    monkeypatch.setattr(
        "maajun.cli.setup.fetch_catalog", lambda base_url, api_key: CATALOG
    )

    setup_model(Replies("2", "1"), config, "openrouter", None, api_key="k")

    assert config.ai.model == "openai/gpt-5.2"


def test_setup_model_asks_for_an_id_when_the_catalogue_cannot_be_read(monkeypatch):
    """An unreachable gateway costs a nicer prompt, not a failed setup."""
    config = Config()
    monkeypatch.setattr("maajun.cli.setup.fetch_catalog", lambda base_url, api_key: ())

    ask = Replies("anthropic/claude-opus-5")
    setup_model(ask, config, "openrouter", None, api_key="k")

    assert config.ai.model == "anthropic/claude-opus-5"
    assert any("e.g." in prompt for prompt in ask.prompts)


def test_an_unpriced_choice_repeats_what_the_gateway_charges(monkeypatch, capsys):
    """Being shown a price and then told there is none reads as a bug."""
    config = Config()
    monkeypatch.setattr(
        "maajun.cli.setup.fetch_catalog", lambda base_url, api_key: CATALOG
    )

    setup_model(Replies("2", "1"), config, "openrouter", None, api_key="k")

    output = flat(capsys.readouterr().out)
    assert "No published price for openai/gpt-5.2" in output
    assert "gateway quotes $1.75 in / $14.00 out" in output


def test_a_free_model_says_free_rather_than_zero_dollars():
    line = catalog_line(entry("z-ai/glm-5.3-flash", "z-ai", 0.0, 0.0))
    assert "free" in line


def test_a_model_the_gateway_does_not_price_says_so():
    line = catalog_line(CatalogEntry(id="x/y", vendor="x", input=None, output=None))
    assert "not quoted" in line


def ruff_checkout(directory):
    """A checkout whose manifests imply `uv run ruff check .`."""
    (directory / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (directory / "uv.lock").write_text("", encoding="utf-8")
    return directory


def test_verification_commands_are_prefilled_from_the_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(ruff_checkout(tmp_path))
    monkeypatch.setattr("maajun.cli.setup.detect_repo_from_git", lambda *a: "acme/api")
    assert ask_verification_commands(
        Asker(interactive=False), "acme/api", None, []
    ) == ["uv run ruff check ."]


def test_configured_commands_are_kept_over_a_fresh_detection(tmp_path, monkeypatch):
    monkeypatch.chdir(ruff_checkout(tmp_path))
    monkeypatch.setattr("maajun.cli.setup.detect_repo_from_git", lambda *a: "acme/api")
    assert ask_verification_commands(
        Asker(interactive=False), "acme/api", None, ["pytest -q"]
    ) == ["pytest -q"]


def test_undetectable_project_leaves_the_prompt_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("maajun.cli.setup.detect_repo_from_git", lambda *a: "acme/api")
    assert ask_verification_commands(Asker(interactive=False), "acme/api", None, []) == []


def test_checkout_candidates_skip_a_directory_for_another_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("maajun.cli.setup.detect_repo_from_git", lambda *a: "other/app")
    entry = RepoConfig(repo="acme/api", deployment=DeploymentConfig(path="/srv/api"))
    assert checkout_candidates("acme/api", entry) == [Path("/srv/api")]
