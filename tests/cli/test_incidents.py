import pytest
from typer.testing import CliRunner

from maajun.cli import app
from maajun.daemon.store import MAX_ATTEMPTS, IncidentStore
from maajun.monitors import ErrorEvent

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path):
    config_path = tmp_path / "config.toml"
    data = tmp_path / "data"
    config_path.write_text(f'[daemon]\nworkdir = "{data}"\n')
    return config_path, data


def open_store(data):
    return IncidentStore(data / "incidents.db")


def add(store, fingerprint, *, message, cost=0.0, failures=0, url="", repo=""):
    store.record(ErrorEvent(
        source="logfile:/x.log", message=message, details=message,
        fingerprint=fingerprint, repo=repo,
    ))
    for _ in range(failures):
        store.mark_failed(fingerprint, repo)
    if url:
        store.mark_processed(fingerprint, repo, branch="", pr_url=url, cost_usd=cost)


def test_no_database_yet_is_explained_not_an_error(workdir):
    config_path, _ = workdir
    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert result.exit_code == 0
    assert "No incidents yet" in result.output


def test_lists_incidents_with_cost_and_url(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="KeyError: discount", cost=0.0123,
         url="https://github.com/o/n/issues/7")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert result.exit_code == 0
    assert "fp1" in result.output
    assert "KeyError" in result.output
    assert "0.0123" in result.output
    assert "#7" in result.output


def test_shows_todays_spend_against_the_cap(workdir, tmp_path):
    config_path, data = workdir
    config_path.write_text(
        f'[daemon]\nworkdir = "{data}"\nmax_usd_per_day = 0.5\n'
    )
    store = open_store(data)
    add(store, "fp1", message="boom", cost=0.25, url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "0.2500" in result.output
    assert "of $0.5 cap" in result.output


def test_shows_the_default_cap_when_none_is_configured(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="boom", cost=0.25, url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "of $5 cap" in result.output


def test_warns_when_the_cap_is_disabled(workdir):
    config_path, data = workdir
    config_path.write_text(f'[daemon]\nworkdir = "{data}"\nmax_usd_per_day = 0\n')
    store = open_store(data)
    add(store, "fp1", message="boom", cost=0.25, url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "no cap set" in result.output


def test_failed_flag_lists_only_exhausted_incidents(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "gone", message="permanently broken", failures=MAX_ATTEMPTS)
    add(store, "fine", message="worked out", url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path), "--failed"])
    assert "gone" in result.output
    assert "fine" not in result.output


def test_exhausted_incidents_are_flagged_in_the_summary(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "gone", message="permanently broken", failures=MAX_ATTEMPTS)
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "no longer retried" in result.output


def test_retryable_failure_shows_its_attempt_count(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "flaky", message="transient", failures=1)
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert f"1/{MAX_ATTEMPTS}" in result.output


def test_limit_caps_the_rows(workdir):
    config_path, data = workdir
    store = open_store(data)
    for n in range(5):
        add(store, f"fp{n}", message=f"error {n}", url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path), "-n", "2"])
    assert result.output.count("$") >= 1  # rendered
    assert sum(f"fp{n}" in result.output for n in range(5)) == 2


def test_limit_caps_the_failed_list_too(workdir):
    """--failed used to return every exhausted incident, ignoring --limit."""
    config_path, data = workdir
    store = open_store(data)
    for n in range(5):
        add(store, f"fp{n}", message=f"error {n}", failures=MAX_ATTEMPTS)
    store.close()

    result = runner.invoke(
        app, ["incidents", "-c", str(config_path), "--failed", "-n", "2"]
    )
    assert sum(f"fp{n}" in result.output for n in range(5)) == 2


# ---------------------------------------------------------------------------
# Telling repos apart
# ---------------------------------------------------------------------------


def test_the_repo_column_appears_once_more_than_one_repo_has_incidents(workdir):
    """Two repos' issues both render as '#1' — the repo is what separates them."""
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="KeyError: user_id", repo="acme/api",
         url="https://github.com/acme/api/issues/1")
    add(store, "fp1", message="KeyError: user_id", repo="acme/web",
         url="https://github.com/acme/web/issues/1")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert result.exit_code == 0
    assert "acme/api" in result.output
    assert "acme/web" in result.output


def test_a_single_repo_install_keeps_the_narrower_table(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="boom", repo="acme/api", url="u")
    add(store, "fp2", message="bang", repo="acme/api", url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "Repo" not in result.output


def test_repo_filter_shows_only_that_repos_incidents(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "apionly", message="api broke", repo="acme/api", url="u")
    add(store, "webonly", message="web broke", repo="acme/web", url="u")
    store.close()

    result = runner.invoke(
        app, ["incidents", "-c", str(config_path), "--repo", "acme/web"]
    )
    assert result.exit_code == 0
    assert "webonly" in result.output
    assert "apionly" not in result.output


def test_repo_filter_on_an_unknown_repo_lists_the_known_ones(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="boom", repo="acme/api", url="u")
    store.close()

    result = runner.invoke(
        app, ["incidents", "-c", str(config_path), "-r", "acme/typo"]
    )
    assert result.exit_code == 0
    assert "No incidents recorded for acme/typo" in result.output
    assert "acme/api" in result.output


def test_repo_filter_narrows_the_failed_list_too(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "gone", message="api gone", repo="acme/api", failures=MAX_ATTEMPTS)
    add(store, "gone", message="web gone", repo="acme/web", failures=MAX_ATTEMPTS)
    store.close()

    result = runner.invoke(
        app, ["incidents", "-c", str(config_path), "--failed", "-r", "acme/web"]
    )
    assert "web gone" in result.output
    assert "api gone" not in result.output


def test_local_mode_incidents_are_labelled_not_left_blank(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "local1", message="on disk", repo="", url="/reports/local1.md")
    add(store, "fp2", message="in github", repo="acme/api", url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "(local)" in result.output


def test_a_reported_issue_is_told_apart_from_a_caught_one(workdir):
    """`maajun report` records an incident too; the list has to say which is
    which, or a report looks like an error a monitor found."""
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="KeyError: discount", url="https://github.com/o/n/issues/7")
    store.record(ErrorEvent(
        source="manual", message="Checkout is slow", details="Checkout is slow",
        fingerprint="fp2",
    ))
    store.mark_processed("fp2", branch="", pr_url="https://github.com/o/n/issues/8")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])

    assert "Caught by" in result.output
    assert "report" in result.output
    assert "logfile" in result.output


def test_one_kind_of_source_needs_no_column(workdir):
    config_path, data = workdir
    store = open_store(data)
    add(store, "fp1", message="KeyError: discount")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])

    assert "Caught by" not in result.output
