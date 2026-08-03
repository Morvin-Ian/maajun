"""Tests for the incident store."""

import pytest

from maajun.monitors import ErrorEvent
from maajun.state import IncidentStore


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def make_event(details="ValueError: boom"):
    return ErrorEvent(source="test", message="boom", details=details)


def test_first_sighting_is_new(store):
    assert store.record(make_event()) is True


def test_repeat_sighting_is_not_new(store):
    event = make_event()
    store.record(event)
    assert store.record(make_event()) is False
    assert store.get(event.fingerprint)["count"] == 2


def test_different_errors_are_separate(store):
    assert store.record(make_event("ValueError: a")) is True
    assert store.record(make_event("KeyError: b")) is True
    assert len(store.all()) == 2


def test_mark_processed(store):
    event = make_event()
    store.record(event)
    store.mark_processed(
        event.fingerprint, branch="maajun/incident-abc", pr_url="https://pr/1"
    )
    row = store.get(event.fingerprint)
    assert row["status"] == "processed"
    assert row["pr_url"] == "https://pr/1"
    assert row["branch"] == "maajun/incident-abc"


def test_mark_failed(store):
    event = make_event()
    store.record(event)
    store.mark_failed(event.fingerprint)
    assert store.get(event.fingerprint)["status"] == "failed"


def test_persists_across_reopen(tmp_path):
    event = make_event()
    s1 = IncidentStore(tmp_path / "incidents.db")
    s1.record(event)
    s1.close()

    s2 = IncidentStore(tmp_path / "incidents.db")
    assert s2.record(make_event()) is False
    s2.close()


def test_mark_processed_with_cost(store):
    event = make_event()
    store.record(event)
    store.mark_processed(
        event.fingerprint,
        branch="maajun/incident-abc",
        pr_url="https://pr/1",
        cost_usd=0.0042,
        prompt_tokens=500,
        completion_tokens=200,
    )
    row = store.get(event.fingerprint)
    assert row["cost_usd"] == 0.0042
    assert row["prompt_tokens"] == 500
    assert row["completion_tokens"] == 200


def test_total_cost(store):
    e1 = make_event("ValueError: a")
    e2 = make_event("KeyError: b")
    store.record(e1)
    store.record(e2)
    store.mark_processed(e1.fingerprint, branch="b1", pr_url="https://pr/1", cost_usd=0.01)
    store.mark_processed(e2.fingerprint, branch="b2", pr_url="https://pr/2", cost_usd=0.02)
    assert abs(store.total_cost() - 0.03) < 0.0001


def test_total_tokens(store):
    e1 = make_event("ValueError: a")
    e2 = make_event("KeyError: b")
    store.record(e1)
    store.record(e2)
    store.mark_processed(e1.fingerprint, branch="b1", pr_url="https://pr/1",
                         prompt_tokens=100, completion_tokens=50)
    store.mark_processed(e2.fingerprint, branch="b2", pr_url="https://pr/2",
                         prompt_tokens=200, completion_tokens=80)
    totals = store.total_tokens()
    assert totals["prompt_tokens"] == 300
    assert totals["completion_tokens"] == 130


def test_forget_drops_incident_so_it_records_as_new(store):
    e = make_event("ValueError: a")
    assert store.record(e) is True
    store.forget(e.fingerprint)
    assert store.get(e.fingerprint) is None
    assert store.record(e) is True


def test_cost_since_only_counts_recent_incidents(tmp_path):
    from maajun.utils import utc_day_start_iso

    store = IncidentStore(tmp_path / "i.db")
    for fingerprint, cost in (("old", 5.0), ("today", 0.25)):
        store.record(ErrorEvent(
            source="t", message=fingerprint, details=fingerprint,
            fingerprint=fingerprint,
        ))
        store.mark_processed(fingerprint, branch="", pr_url="x", cost_usd=cost)
    store._conn.execute(
        "UPDATE incidents SET last_seen = ? WHERE fingerprint = ?",
        ("2020-01-01T00:00:00+00:00", "old"),
    )
    store._conn.commit()

    assert store.cost_since(utc_day_start_iso()) == 0.25
    assert store.total_cost() == 5.25


# ---------------------------------------------------------------------------
# Failed-incident retry
# ---------------------------------------------------------------------------


def _event(fingerprint="fp1"):
    return ErrorEvent(
        source="t", message="boom", details="boom", fingerprint=fingerprint,
    )


def test_failed_incident_is_retried(tmp_path):
    """Regression: a transient GitHub 502 permanently blacklisted the error."""
    store = IncidentStore(tmp_path / "i.db")
    assert store.record(_event()) is True
    store.mark_failed("fp1")

    assert store.record(_event()) is True  # retried, not skipped


def test_retries_stop_after_max_attempts(tmp_path):
    from maajun.state import MAX_ATTEMPTS

    store = IncidentStore(tmp_path / "i.db")
    store.record(_event())
    for _ in range(MAX_ATTEMPTS):
        store.mark_failed("fp1")
        store.record(_event())

    assert store.record(_event()) is False
    assert store.get("fp1")["attempts"] == MAX_ATTEMPTS


def test_processed_incident_is_never_retried(tmp_path):
    store = IncidentStore(tmp_path / "i.db")
    store.record(_event())
    store.mark_processed("fp1", branch="", pr_url="u")

    assert store.record(_event()) is False


def test_repeat_sighting_still_bumps_the_counter(tmp_path):
    store = IncidentStore(tmp_path / "i.db")
    store.record(_event())
    store.mark_processed("fp1", branch="", pr_url="u")
    store.record(_event())
    store.record(_event())

    assert store.get("fp1")["count"] == 3


def test_exhausted_lists_permanently_failed_incidents(tmp_path):
    from maajun.state import MAX_ATTEMPTS

    store = IncidentStore(tmp_path / "i.db")
    store.record(_event("gone"))
    for _ in range(MAX_ATTEMPTS):
        store.mark_failed("gone")
    store.record(_event("fine"))
    store.mark_processed("fine", branch="", pr_url="u")

    assert [row["fingerprint"] for row in store.exhausted()] == ["gone"]


def test_database_from_an_older_version_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS won't add a new column to an existing table."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE incidents (fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL,"
        " message TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
        " count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',"
        " branch TEXT, pr_url TEXT, cost_usd REAL DEFAULT 0,"
        " prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO incidents VALUES"
        " ('old1','s','m','t','t',1,'failed','','',0.5,0,0)"
    )
    conn.commit()
    conn.close()

    store = IncidentStore(path)
    assert store.get("old1")["attempts"] == 0
    assert store.record(_event("old1")) is True  # eligible for retry
