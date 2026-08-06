"""Tests for the incident store."""

import pytest

from maajun.daemon.store import IncidentStore
from maajun.monitors import ErrorEvent


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
    from maajun.daemon.store import MAX_ATTEMPTS

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
    from maajun.daemon.store import MAX_ATTEMPTS

    store = IncidentStore(tmp_path / "i.db")
    store.record(_event("gone"))
    for _ in range(MAX_ATTEMPTS):
        store.mark_failed("gone")
    store.record(_event("fine"))
    store.mark_processed("fine", branch="", pr_url="u")

    assert [row["fingerprint"] for row in store.exhausted()] == ["gone"]


# ---------------------------------------------------------------------------
# Repo scoping
# ---------------------------------------------------------------------------


def _in_repo(repo, details="ValueError: boom"):
    return ErrorEvent(source="test", message="boom", details=details, repo=repo)


def test_the_same_error_in_two_repos_is_two_incidents(store):
    """Regression: whichever repo was polled first claimed the error.

    Two services sharing a library hit the identical traceback, and the second
    repo's copy was silently dropped as already known — so it never got an
    issue.
    """
    assert store.record(_in_repo("acme/api")) is True
    assert store.record(_in_repo("acme/web")) is True
    assert sorted(row["repo"] for row in store.all()) == ["acme/api", "acme/web"]


def test_a_repeat_in_one_repo_is_still_a_repeat(store):
    assert store.record(_in_repo("acme/api")) is True
    assert store.record(_in_repo("acme/api")) is False


def test_marking_one_repo_processed_leaves_the_other_new(store):
    api, web = _in_repo("acme/api"), _in_repo("acme/web")
    store.record(api)
    store.record(web)
    store.mark_processed(api.fingerprint, "acme/api", branch="", pr_url="u")

    assert store.get(api.fingerprint, "acme/api")["status"] == "processed"
    assert store.get(web.fingerprint, "acme/web")["status"] == "new"


def test_failing_in_one_repo_does_not_burn_the_others_retries(store):
    api, web = _in_repo("acme/api"), _in_repo("acme/web")
    store.record(api)
    store.record(web)
    store.mark_failed(api.fingerprint, "acme/api")

    assert store.get(api.fingerprint, "acme/api")["attempts"] == 1
    assert store.get(web.fingerprint, "acme/web")["attempts"] == 0


def test_forget_only_drops_the_named_repos_copy(store):
    api, web = _in_repo("acme/api"), _in_repo("acme/web")
    store.record(api)
    store.record(web)
    store.forget(api.fingerprint, "acme/api")

    assert store.get(api.fingerprint, "acme/api") is None
    assert store.get(web.fingerprint, "acme/web") is not None


def test_all_can_be_filtered_to_one_repo(store):
    store.record(_in_repo("acme/api", "ValueError: a"))
    store.record(_in_repo("acme/web", "KeyError: b"))
    store.record(_in_repo("acme/web", "TypeError: c"))

    assert len(store.all()) == 3
    assert len(store.all("acme/web")) == 2
    assert store.all("acme/api")[0]["message"] == "boom"


def test_repos_lists_what_has_incidents(store):
    store.record(_in_repo("acme/web", "ValueError: a"))
    store.record(_in_repo("acme/api", "KeyError: b"))

    assert store.repos() == ["acme/api", "acme/web"]


def test_local_mode_incidents_record_an_empty_repo(store):
    event = make_event()
    store.record(event)

    assert store.all()[0]["repo"] == ""
    assert store.get(event.fingerprint) is not None


def test_database_from_an_older_version_is_rejected(tmp_path):
    """No conversion: an outdated schema fails at open, naming the fix.

    CREATE TABLE IF NOT EXISTS leaves the old table alone, so without this the
    first query naming a new column would blow up mid-incident instead.
    """
    import sqlite3

    from maajun.daemon.store import StoreError

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE incidents (fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL,"
        " message TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
        " count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',"
        " branch TEXT, pr_url TEXT, cost_usd REAL DEFAULT 0,"
        " prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(StoreError) as exc:
        IncidentStore(path)
    message = str(exc.value)
    assert "older version" in message
    assert "attempts" in message and "repo" in message  # names what is missing
    assert "Delete it" in message


def test_a_database_missing_only_one_column_is_still_rejected(tmp_path):
    """Half-converted is not usable either — no partial upgrades."""
    import sqlite3

    from maajun.daemon.store import StoreError

    path = tmp_path / "half.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE incidents (fingerprint TEXT NOT NULL, repo TEXT NOT NULL"
        " DEFAULT '', source TEXT NOT NULL, message TEXT NOT NULL,"
        " first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
        " count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',"
        " branch TEXT, pr_url TEXT, cost_usd REAL DEFAULT 0,"
        " prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,"
        " PRIMARY KEY (fingerprint, repo))"
    )
    conn.commit()
    conn.close()

    with pytest.raises(StoreError, match="attempts"):
        IncidentStore(path)


def test_a_fresh_database_reopens_cleanly(tmp_path):
    path = tmp_path / "i.db"
    first = IncidentStore(path)
    first.record(_in_repo("acme/api"))
    first.close()

    second = IncidentStore(path)
    assert [row["repo"] for row in second.all()] == ["acme/api"]
    assert second.record(_in_repo("acme/api")) is False
    second.close()
