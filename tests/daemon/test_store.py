import pytest

from maajun.daemon.store import (
    ARTIFACT_ISSUE,
    ARTIFACT_PR,
    ARTIFACT_REPORT,
    IncidentStore,
)
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


def test_repeat_sighting_of_a_handled_error_is_not_new(store):
    """Handled, not merely recorded, is what stops an error being re-reported."""
    event = make_event()
    store.record(event)
    store.mark_processed(event.fingerprint, branch="", pr_url="u")

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
    s1.mark_processed(event.fingerprint, branch="", pr_url="u")
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
    store.conn.execute(
        "UPDATE incidents SET last_seen = ? WHERE fingerprint = ?",
        ("2020-01-01T00:00:00+00:00", "old"),
    )
    store.conn.commit()

    assert store.cost_since(utc_day_start_iso()) == 0.25
    assert store.total_cost() == 5.25


# ---------------------------------------------------------------------------
# Failed-incident retry
# ---------------------------------------------------------------------------


def fingerprinted_event(fingerprint="fp1"):
    return ErrorEvent(
        source="t", message="boom", details="boom", fingerprint=fingerprint,
    )


def test_failed_incident_is_retried(tmp_path):
    """Regression: a transient GitHub 502 permanently blacklisted the error."""
    store = IncidentStore(tmp_path / "i.db")
    assert store.record(fingerprinted_event()) is True
    store.mark_failed("fp1")

    assert store.record(fingerprinted_event()) is True  # retried, not skipped


def test_retries_stop_after_max_attempts(tmp_path):
    from maajun.daemon.store import MAX_ATTEMPTS

    store = IncidentStore(tmp_path / "i.db")
    store.record(fingerprinted_event())
    for _ in range(MAX_ATTEMPTS):
        store.mark_failed("fp1")
        store.record(fingerprinted_event())

    assert store.record(fingerprinted_event()) is False
    assert store.get("fp1")["attempts"] == MAX_ATTEMPTS


def test_processed_incident_is_never_retried(tmp_path):
    store = IncidentStore(tmp_path / "i.db")
    store.record(fingerprinted_event())
    store.mark_processed("fp1", branch="", pr_url="u")

    assert store.record(fingerprinted_event()) is False


def test_repeat_sighting_still_bumps_the_counter(tmp_path):
    store = IncidentStore(tmp_path / "i.db")
    store.record(fingerprinted_event())
    store.mark_processed("fp1", branch="", pr_url="u")
    store.record(fingerprinted_event())
    store.record(fingerprinted_event())

    assert store.get("fp1")["count"] == 3


def test_exhausted_lists_permanently_failed_incidents(tmp_path):
    from maajun.daemon.store import MAX_ATTEMPTS

    store = IncidentStore(tmp_path / "i.db")
    store.record(fingerprinted_event("gone"))
    for _ in range(MAX_ATTEMPTS):
        store.mark_failed("gone")
    store.record(fingerprinted_event("fine"))
    store.mark_processed("fine", branch="", pr_url="u")

    assert [row["fingerprint"] for row in store.exhausted()] == ["gone"]


# ---------------------------------------------------------------------------
# Repo scoping
# ---------------------------------------------------------------------------


def in_repo(repo, details="ValueError: boom", fingerprint=""):
    return ErrorEvent(
        source="test", message="boom", details=details, repo=repo,
        fingerprint=fingerprint,
    )


def test_the_same_error_in_two_repos_is_two_incidents(store):
    """Regression: whichever repo was polled first claimed the error.

    Two services sharing a library hit the identical traceback, and the second
    repo's copy was silently dropped as already known — so it never got an
    issue.
    """
    assert store.record(in_repo("acme/api")) is True
    assert store.record(in_repo("acme/web")) is True
    assert sorted(row["repo"] for row in store.all()) == ["acme/api", "acme/web"]


def test_a_repeat_in_one_repo_is_still_a_repeat(store):
    event = in_repo("acme/api")
    assert store.record(event) is True
    store.mark_processed(event.fingerprint, "acme/api", branch="", pr_url="u")

    assert store.record(in_repo("acme/api")) is False


def test_marking_one_repo_processed_leaves_the_other_new(store):
    api, web = in_repo("acme/api"), in_repo("acme/web")
    store.record(api)
    store.record(web)
    store.mark_processed(api.fingerprint, "acme/api", branch="", pr_url="u")

    assert store.get(api.fingerprint, "acme/api")["status"] == "processed"
    assert store.get(web.fingerprint, "acme/web")["status"] == "new"


def test_failing_in_one_repo_does_not_burn_the_others_retries(store):
    api, web = in_repo("acme/api"), in_repo("acme/web")
    store.record(api)
    store.record(web)
    store.mark_failed(api.fingerprint, "acme/api")

    assert store.get(api.fingerprint, "acme/api")["attempts"] == 1
    assert store.get(web.fingerprint, "acme/web")["attempts"] == 0


def test_forget_only_drops_the_named_repos_copy(store):
    api, web = in_repo("acme/api"), in_repo("acme/web")
    store.record(api)
    store.record(web)
    store.forget(api.fingerprint, "acme/api")

    assert store.get(api.fingerprint, "acme/api") is None
    assert store.get(web.fingerprint, "acme/web") is not None


def test_all_can_be_filtered_to_one_repo(store):
    store.record(in_repo("acme/api", "ValueError: a"))
    store.record(in_repo("acme/web", "KeyError: b"))
    store.record(in_repo("acme/web", "TypeError: c"))

    assert len(store.all()) == 3
    assert len(store.all("acme/web")) == 2
    assert store.all("acme/api")[0]["message"] == "boom"


def test_repos_lists_what_has_incidents(store):
    store.record(in_repo("acme/web", "ValueError: a"))
    store.record(in_repo("acme/api", "KeyError: b"))

    assert store.repos() == ["acme/api", "acme/web"]


def test_local_mode_incidents_record_an_empty_repo(store):
    event = make_event()
    store.record(event)

    assert store.all()[0]["repo"] == ""
    assert store.get(event.fingerprint) is not None


def legacy_single_repo_db(path):
    """A database from before multi-repo support: no repo, no attempts."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE incidents (fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL,"
        " message TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
        " count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',"
        " branch TEXT, pr_url TEXT, cost_usd REAL DEFAULT 0,"
        " prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO incidents (fingerprint, source, message, first_seen,"
        " last_seen, count, status, pr_url, cost_usd)"
        " VALUES ('abc123', 'logfile:/x.log', 'KeyError', 't0', 't1', 4,"
        " 'processed', 'https://github.com/a/b/pull/7', 0.25)"
    )
    conn.commit()
    conn.close()


def test_an_older_database_is_migrated_not_rejected(tmp_path):
    """History survives the upgrade — it used to have to be deleted."""
    path = tmp_path / "old.db"
    legacy_single_repo_db(path)

    store = IncidentStore(path)
    rows = store.all()
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "abc123"
    assert rows[0]["count"] == 4
    assert rows[0]["cost_usd"] == 0.25
    assert rows[0]["pr_url"] == "https://github.com/a/b/pull/7"
    # Columns the old schema never had take their declared defaults.
    assert rows[0]["repo"] == ""
    assert rows[0]["attempts"] == 0
    store.close()


def test_a_migrated_database_is_fully_usable(tmp_path):
    """The rebuilt table carries the new primary key, not just the columns."""
    path = tmp_path / "old.db"
    legacy_single_repo_db(path)

    store = IncidentStore(path)
    # Same fingerprint, different repo: only possible with PK (fingerprint, repo).
    assert store.record(in_repo("acme/api", fingerprint="abc123")) is True
    assert store.get("abc123", "acme/api") is not None
    assert store.get("abc123", "") is not None
    store.mark_failed("abc123", "acme/api")
    assert store.get("abc123", "acme/api")["attempts"] == 1
    store.close()


def test_migration_is_not_reapplied_on_reopen(tmp_path):
    path = tmp_path / "old.db"
    legacy_single_repo_db(path)

    first = IncidentStore(path)
    first.record(in_repo("acme/api"))
    first.close()

    second = IncidentStore(path)
    assert len(second.all()) == 2  # the migrated row plus the new one
    second.close()


def test_a_database_missing_only_one_column_is_upgraded_in_place(tmp_path):
    """A half-converted schema gains the column rather than failing to open."""
    import sqlite3

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

    store = IncidentStore(path)
    store.record(in_repo("acme/api"))
    assert store.all()[0]["attempts"] == 0
    store.close()


def test_a_database_from_a_newer_maajun_is_refused(tmp_path):
    """Downgrading must not silently rewrite a schema it does not understand."""
    import sqlite3

    from maajun.daemon.store import SCHEMA_VERSION, StoreError

    path = tmp_path / "future.db"
    IncidentStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()

    with pytest.raises(StoreError) as exc:
        IncidentStore(path)
    assert "newer version of maajun" in str(exc.value)


def test_a_fresh_database_reopens_cleanly(tmp_path):
    path = tmp_path / "i.db"
    first = IncidentStore(path)
    event = in_repo("acme/api")
    first.record(event)
    first.mark_processed(event.fingerprint, "acme/api", branch="", pr_url="u")
    first.close()

    second = IncidentStore(path)
    assert [row["repo"] for row in second.all()] == ["acme/api"]
    assert second.record(in_repo("acme/api")) is False
    second.close()


# ---------------------------------------------------------------------------
# report text and artifact kind
# ---------------------------------------------------------------------------


def test_mark_processed_keeps_the_report_and_what_it_produced(store):
    store.record(in_repo("acme/api", fingerprint="fp1"))
    store.mark_processed(
        "fp1", "acme/api",
        branch="maajun/incident-fp1",
        pr_url="https://github.com/acme/api/pull/3",
        report_text="# KeyError\n\n## Root cause\ncart/totals.py:88",
        artifact_kind=ARTIFACT_PR,
    )
    row = store.get("fp1", "acme/api")
    assert "cart/totals.py:88" in row["report_text"]
    assert row["artifact_kind"] == ARTIFACT_PR


def test_report_text_defaults_to_empty_rather_than_null(store):
    """Recall joins on this column; NULL would need handling at every reader."""
    store.record(in_repo("acme/api", fingerprint="fp1"))
    row = store.get("fp1", "acme/api")
    assert row["report_text"] == ""
    assert row["artifact_kind"] == ""


def test_migration_backfills_artifact_kind_from_the_old_columns(tmp_path):
    """Pre-existing rows get a kind inferred from branch and URL shape."""
    import sqlite3

    path = tmp_path / "old.db"
    legacy_single_repo_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO incidents (fingerprint, source, message, first_seen,"
        " last_seen, status, branch, pr_url) VALUES ('fixed', 's', 'm', 't', 't',"
        " 'processed', 'maajun/incident-fixed', 'https://github.com/a/b/pull/9')"
    )
    conn.execute(
        "INSERT INTO incidents (fingerprint, source, message, first_seen,"
        " last_seen, status, pr_url) VALUES ('local', 's', 'm', 't', 't',"
        " 'processed', '/home/me/.local/share/maajun/reports/local.md')"
    )
    conn.commit()
    conn.close()

    store = IncidentStore(path)
    kinds = {row["fingerprint"]: row["artifact_kind"] for row in store.all()}
    # A branch means fix mode pushed one.
    assert kinds["fixed"] == ARTIFACT_PR
    # An http URL with no branch is suggest mode's issue.
    assert kinds["abc123"] == ARTIFACT_ISSUE
    # A filesystem path is a local-mode report.
    assert kinds["local"] == ARTIFACT_REPORT
    store.close()


def test_backfill_leaves_unprocessed_rows_without_a_kind(tmp_path):
    """A 'new' incident has produced nothing yet — don't invent an artifact."""
    path = tmp_path / "old.db"
    legacy_single_repo_db(path)
    store = IncidentStore(path)
    store.record(in_repo("acme/api", fingerprint="pending"))
    assert store.get("pending", "acme/api")["artifact_kind"] == ""
    store.close()


# ---------------------------------------------------------------------------
# Incidents recorded but never handled
# ---------------------------------------------------------------------------


def test_an_unhandled_incident_is_picked_up_again(store):
    """'new' means recorded, not published — deferred or interrupted."""
    assert store.record(fingerprinted_event()) is True
    assert store.record(fingerprinted_event()) is True  # still unhandled


def test_an_unhandled_incident_keeps_accumulating_sightings(store):
    """Regression: the deferral path deleted the row, so count reset each poll."""
    for _ in range(4):
        store.record(fingerprinted_event())

    assert store.get("fp1")["count"] == 4


def test_first_seen_survives_repeated_deferral(store):
    """It used to become whenever the cap lifted, not when the error started."""
    store.record(fingerprinted_event())
    started = store.get("fp1")["first_seen"]
    for _ in range(3):
        store.record(fingerprinted_event())

    assert store.get("fp1")["first_seen"] == started


def test_an_incident_processed_after_deferral_settles(store):
    store.record(fingerprinted_event())
    store.record(fingerprinted_event())
    store.mark_processed("fp1", branch="", pr_url="u")

    assert store.record(fingerprinted_event()) is False


def test_chat_messages_written_before_the_index_are_still_searchable(tmp_path):
    """The index has to be backfilled, or every old conversation goes missing."""
    import sqlite3

    from maajun.chat.memory import ChatMemory
    from maajun.daemon.store import CHAT_SCHEMA, SCHEMA, has_fts

    path = tmp_path / "pre-fts.db"
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    for statement in CHAT_SCHEMA:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO chat_sessions (started_at, updated_at, title)"
        " VALUES ('t0', 't0', 'checkout')"
    )
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at)"
        " VALUES (1, 'assistant', 'the checkout 500 was a KeyError', 't0')"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    memory = ChatMemory(path)
    assert has_fts(memory.conn)
    assert len(memory.search("checkout KeyError")) == 1
    memory.close()


def test_the_index_follows_new_and_deleted_messages(tmp_path):
    from maajun.chat.memory import ChatMemory

    memory = ChatMemory(tmp_path / "incidents.db")
    session = memory.start_session()
    memory.add_message(session, "user", "a timeout on payments")
    assert len(memory.search("timeout payments")) == 1

    memory.delete_session(session)
    assert memory.search("timeout payments") == []
    memory.close()


# ---------------------------------------------------------------------------
# Spend banked by an attempt that never finished
# ---------------------------------------------------------------------------


def test_add_spend_accumulates_rather_than_overwriting(store):
    """mark_processed sets the totals for a finished incident; a failed
    attempt adds to them, so three failed retries cost three times."""
    event = make_event()
    store.record(event)
    store.add_spend(
        event.fingerprint, prompt_tokens=1000, completion_tokens=200, cost_usd=0.004
    )
    store.add_spend(
        event.fingerprint, prompt_tokens=500, completion_tokens=100, cost_usd=0.002
    )
    row = store.get(event.fingerprint)
    assert row["prompt_tokens"] == 1500
    assert row["completion_tokens"] == 300
    assert row["cost_usd"] == pytest.approx(0.006)


def test_spend_from_a_failed_attempt_counts_toward_the_cap(store):
    """The whole point: cost_since backs daemon.max_usd_per_day, and an
    analysis that died on its thirtieth tool round still cost thirty calls."""
    event = make_event()
    store.record(event)
    store.mark_failed(event.fingerprint)
    store.add_spend(event.fingerprint, prompt_tokens=9000, cost_usd=1.25)
    assert store.cost_since("1970-01-01T00:00:00Z") == pytest.approx(1.25)


def test_add_spend_of_nothing_is_a_no_op(store):
    """A turn that failed before the first response reported no usage."""
    event = make_event()
    store.record(event)
    store.add_spend(event.fingerprint, prompt_tokens=1, cost_usd=0.5)
    store.add_spend(event.fingerprint)
    assert store.get(event.fingerprint)["cost_usd"] == pytest.approx(0.5)


def test_add_spend_for_an_unrecorded_incident_is_harmless(store):
    """A manual report has no row; the update matches nothing, as with
    mark_processed on that path."""
    store.add_spend("never-seen", cost_usd=1.0)
    assert store.total_cost() == 0


# ---------------------------------------------------------------------------
# Full-text index repair
# ---------------------------------------------------------------------------


def test_a_database_upgraded_without_fts5_is_indexed_on_a_later_open(tmp_path):
    """migrate_to_4 tolerates a SQLite with no FTS5, and bumping user_version
    would otherwise mark the file done with no index — permanently."""
    from maajun.chat.memory import ChatMemory
    from maajun.daemon.store import connect, has_fts

    path = tmp_path / "incidents.db"
    conn = connect(path)
    for trigger in ("chat_messages_ai", "chat_messages_ad", "chat_messages_au"):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP TABLE chat_messages_fts")
    conn.commit()
    from maajun.daemon.store import SCHEMA_VERSION

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert not has_fts(conn)
    conn.close()

    memory = ChatMemory(path)
    assert memory.fts, "the index should be rebuilt on open, not skipped forever"

    session = memory.start_session()
    memory.add_message(session, "user", "the checkout flow raised a KeyError")
    # Words in any order is the thing LIKE could not do.
    assert memory.search("KeyError checkout", exclude_session=0)
    memory.close()


def test_messages_written_while_unindexed_are_searchable_after_repair(tmp_path):
    """The repair rebuilds from chat_messages, so nothing written in the
    meantime is lost to search."""
    from maajun.chat.memory import ChatMemory
    from maajun.daemon.store import connect

    path = tmp_path / "incidents.db"
    conn = connect(path)
    for trigger in ("chat_messages_ai", "chat_messages_ad", "chat_messages_au"):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP TABLE chat_messages_fts")
    conn.commit()
    conn.close()

    unindexed = ChatMemory(path)
    # Undo the repair this open just performed, to stand in for a machine
    # whose SQLite has no FTS5 at all: no index, and no triggers feeding it.
    for trigger in ("chat_messages_ai", "chat_messages_ad", "chat_messages_au"):
        unindexed.conn.execute(f"DROP TRIGGER {trigger}")
    unindexed.conn.execute("DROP TABLE chat_messages_fts")
    unindexed.conn.commit()
    unindexed.fts = False
    session = unindexed.start_session()
    unindexed.add_message(session, "user", "a TimeoutError in the payments worker")
    unindexed.close()

    repaired = ChatMemory(path)
    assert repaired.fts
    assert repaired.search("payments TimeoutError", exclude_session=0)
    repaired.close()


# ---------------------------------------------------------------------------
# An error that comes back after being fixed
# ---------------------------------------------------------------------------


def published(store, fingerprint="fp1", url="https://github.com/o/n/issues/1"):
    event = ErrorEvent(
        source="logfile:/x.log", message="KeyError: cart",
        details="KeyError: cart", fingerprint=fingerprint,
    )
    store.record(event)
    store.mark_processed(fingerprint, "", branch="", pr_url=url)
    return event


def went_quiet_for(store, days: float) -> None:
    from datetime import UTC, datetime, timedelta

    when = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE incidents SET last_seen = ?", (when,))
    store.conn.commit()


def test_an_error_that_keeps_happening_is_not_reported_again(tmp_path):
    """Spamming an unfixed bug helps nobody: its last_seen never goes stale."""
    store = IncidentStore(tmp_path / "i.db")
    event = published(store)

    assert store.record(event) is False
    assert store.record(event) is False


def test_an_error_that_comes_back_after_a_gap_is_reported_again(tmp_path):
    """It stopped, so someone fixed it; it started, so the fix did not hold."""
    store = IncidentStore(tmp_path / "i.db", reopen_after_days=7)
    event = published(store)
    went_quiet_for(store, days=10)

    assert store.record(event) is True
    row = store.get(event.fingerprint)
    assert row["status"] == "new"
    assert row["previous_url"] == "https://github.com/o/n/issues/1"
    assert row["reopened_at"]


def test_the_history_is_kept_across_a_reopen(tmp_path):
    """The count and first_seen are the evidence that it came back."""
    store = IncidentStore(tmp_path / "i.db", reopen_after_days=7)
    event = published(store)
    first_seen = store.get(event.fingerprint)["first_seen"]
    went_quiet_for(store, days=10)

    store.record(event)

    row = store.get(event.fingerprint)
    assert row["first_seen"] == first_seen
    assert row["count"] == 2
    assert row["attempts"] == 0  # a fresh run, not a continued retry


def test_a_gap_shorter_than_the_window_is_not_a_regression(tmp_path):
    store = IncidentStore(tmp_path / "i.db", reopen_after_days=7)
    event = published(store)
    went_quiet_for(store, days=3)

    assert store.record(event) is False


def test_reopening_can_be_turned_off(tmp_path):
    """0 keeps the old behaviour: each error is reported once, ever."""
    store = IncidentStore(tmp_path / "i.db", reopen_after_days=0)
    event = published(store)
    went_quiet_for(store, days=400)

    assert store.record(event) is False


def test_an_unreadable_last_seen_does_not_reopen(tmp_path):
    """A hand-edited row should not turn into a surprise issue."""
    store = IncidentStore(tmp_path / "i.db", reopen_after_days=7)
    event = published(store)
    store.conn.execute("UPDATE incidents SET last_seen = 'whenever'")
    store.conn.commit()

    assert store.record(event) is False


def test_forgetting_an_incident_makes_it_new_again(tmp_path):
    """For a fix you trust: report it the moment it comes back, not in a week."""
    store = IncidentStore(tmp_path / "i.db")
    event = published(store)

    assert store.forget_artifact(event.fingerprint) is True
    assert store.record(event) is True
    assert store.get(event.fingerprint)["status"] == "new"


def test_forgetting_something_unknown_says_so(tmp_path):
    store = IncidentStore(tmp_path / "i.db")

    assert store.forget_artifact("nosuchfingerprint") is False


def test_a_reopened_incident_is_scoped_to_its_repo(tmp_path):
    """One repo's regression is not another's."""
    store = IncidentStore(tmp_path / "i.db", reopen_after_days=7)
    for repo in ("acme/api", "acme/web"):
        event = ErrorEvent(
            source="logfile:/x.log", message="KeyError", details="KeyError: cart",
            fingerprint="shared", repo=repo,
        )
        store.record(event)
        store.mark_processed("shared", repo, branch="", pr_url=f"https://x/{repo}")
    store.conn.execute(
        "UPDATE incidents SET last_seen = '2020-01-01T00:00:00+00:00'"
        " WHERE repo = 'acme/api'"
    )
    store.conn.commit()

    back = ErrorEvent(
        source="logfile:/x.log", message="KeyError", details="KeyError: cart",
        fingerprint="shared", repo="acme/api",
    )
    assert store.record(back) is True
    assert store.get("shared", "acme/web")["status"] == "processed"
