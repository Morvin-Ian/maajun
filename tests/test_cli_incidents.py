"""Tests for `maajun incidents`."""

import pytest
from typer.testing import CliRunner

from maajun.cli import app
from maajun.monitors import ErrorEvent
from maajun.state import MAX_ATTEMPTS, IncidentStore

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path):
    config_path = tmp_path / "config.toml"
    data = tmp_path / "data"
    config_path.write_text(f'[daemon]\nworkdir = "{data}"\n')
    return config_path, data


def _store(data):
    return IncidentStore(data / "incidents.db")


def _add(store, fingerprint, *, message, cost=0.0, failures=0, url=""):
    store.record(ErrorEvent(
        source="logfile:/x.log", message=message, details=message,
        fingerprint=fingerprint,
    ))
    for _ in range(failures):
        store.mark_failed(fingerprint)
    if url:
        store.mark_processed(fingerprint, branch="", pr_url=url, cost_usd=cost)


def test_no_database_yet_is_explained_not_an_error(workdir):
    config_path, _ = workdir
    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert result.exit_code == 0
    assert "No incidents yet" in result.output


def test_lists_incidents_with_cost_and_url(workdir):
    config_path, data = workdir
    store = _store(data)
    _add(store, "fp1", message="KeyError: discount", cost=0.0123,
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
    store = _store(data)
    _add(store, "fp1", message="boom", cost=0.25, url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "0.2500" in result.output
    assert "of $0.5 cap" in result.output


def test_warns_when_no_cap_is_set(workdir):
    config_path, data = workdir
    store = _store(data)
    _add(store, "fp1", message="boom", cost=0.25, url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "no cap set" in result.output


def test_failed_flag_lists_only_exhausted_incidents(workdir):
    config_path, data = workdir
    store = _store(data)
    _add(store, "gone", message="permanently broken", failures=MAX_ATTEMPTS)
    _add(store, "fine", message="worked out", url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path), "--failed"])
    assert "gone" in result.output
    assert "fine" not in result.output


def test_exhausted_incidents_are_flagged_in_the_summary(workdir):
    config_path, data = workdir
    store = _store(data)
    _add(store, "gone", message="permanently broken", failures=MAX_ATTEMPTS)
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert "no longer retried" in result.output


def test_retryable_failure_shows_its_attempt_count(workdir):
    config_path, data = workdir
    store = _store(data)
    _add(store, "flaky", message="transient", failures=1)
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path)])
    assert f"1/{MAX_ATTEMPTS}" in result.output


def test_limit_caps_the_rows(workdir):
    config_path, data = workdir
    store = _store(data)
    for n in range(5):
        _add(store, f"fp{n}", message=f"error {n}", url="u")
    store.close()

    result = runner.invoke(app, ["incidents", "-c", str(config_path), "-n", "2"])
    assert result.output.count("$") >= 1  # rendered
    assert sum(f"fp{n}" in result.output for n in range(5)) == 2
