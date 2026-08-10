"""Tests for chat session memory."""

import pytest

from maajun.chat.memory import ChatMemory
from maajun.daemon.store import IncidentStore


@pytest.fixture
def memory(tmp_path):
    m = ChatMemory(tmp_path / "incidents.db")
    yield m
    m.close()


def test_a_session_records_its_messages_in_order(memory):
    session = memory.start_session()
    memory.add_message(session, "user", "why did checkout 500?")
    memory.add_message(session, "assistant", "an empty cart hits a KeyError")

    roles = [m["role"] for m in memory.messages(session)]
    assert roles == ["user", "assistant"]
    assert memory.messages(session)[1]["content"].startswith("an empty cart")


def test_the_first_user_message_titles_the_session(memory):
    session = memory.start_session()
    memory.add_message(session, "user", "why did checkout 500?")
    memory.add_message(session, "user", "and what about payments?")

    assert memory.session(session)["title"] == "why did checkout 500?"


def test_an_explicit_title_is_not_overwritten(memory):
    session = memory.start_session(title="checkout triage")
    memory.add_message(session, "user", "why did checkout 500?")

    assert memory.session(session)["title"] == "checkout triage"


def test_a_long_first_message_is_truncated_into_the_title(memory):
    from maajun.chat.memory import TITLE_LENGTH

    session = memory.start_session()
    memory.add_message(session, "user", "x" * 500)

    assert len(memory.session(session)["title"]) == TITLE_LENGTH


def test_a_multiline_first_message_titles_on_one_line(memory):
    session = memory.start_session()
    memory.add_message(session, "user", "first line\nsecond line")

    assert "\n" not in memory.session(session)["title"]


def test_messages_limit_returns_the_newest_still_in_order(memory):
    session = memory.start_session()
    for n in range(6):
        memory.add_message(session, "user", f"message {n}")

    recent = memory.messages(session, limit=2)
    assert [m["content"] for m in recent] == ["message 4", "message 5"]


def test_recent_sessions_are_newest_first_with_counts(memory):
    first = memory.start_session()
    memory.add_message(first, "user", "one")
    second = memory.start_session()
    memory.add_message(second, "user", "two")
    memory.add_message(second, "assistant", "three")

    sessions = memory.recent_sessions()
    assert [s["id"] for s in sessions] == [second, first]
    assert sessions[0]["message_count"] == 2


def test_an_empty_session_still_lists_with_a_zero_count(memory):
    session = memory.start_session()
    listed = memory.recent_sessions()
    assert [s["id"] for s in listed] == [session]
    assert listed[0]["message_count"] == 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_finds_a_message_across_sessions(memory):
    old = memory.start_session()
    memory.add_message(old, "assistant", "the discount key is missing on old carts")
    new = memory.start_session()
    memory.add_message(new, "user", "unrelated")

    hits = memory.search("discount")
    assert len(hits) == 1
    assert hits[0]["session_id"] == old
    assert "discount" in hits[0]["snippet"]


def test_search_is_case_insensitive(memory):
    session = memory.start_session()
    memory.add_message(session, "assistant", "A KeyError on checkout")

    assert len(memory.search("keyerror")) == 1


def test_search_can_exclude_the_current_session(memory):
    """Recall is for past conversations; the live one is already in context."""
    old = memory.start_session()
    memory.add_message(old, "user", "checkout bug")
    current = memory.start_session()
    memory.add_message(current, "user", "checkout bug")

    hits = memory.search("checkout", exclude_session=current)
    assert [h["session_id"] for h in hits] == [old]


def test_search_treats_wildcards_as_literal_text(memory):
    """A bare % must not match every message ever sent."""
    session = memory.start_session()
    memory.add_message(session, "user", "nothing special here")
    memory.add_message(session, "user", "a literal 100% match")

    assert len(memory.search("%")) == 1
    assert "100%" in memory.search("%")[0]["snippet"]


def test_search_treats_underscore_as_literal_text(memory):
    session = memory.start_session()
    memory.add_message(session, "user", "abc")
    memory.add_message(session, "user", "a_c")

    assert len(memory.search("a_c")) == 1


def test_an_empty_query_matches_nothing(memory):
    session = memory.start_session()
    memory.add_message(session, "user", "something")

    assert memory.search("") == []
    assert memory.search("   ") == []


def test_a_long_message_is_snipped_around_the_match(memory):
    from maajun.chat.memory import SNIPPET_LENGTH

    session = memory.start_session()
    memory.add_message(session, "assistant", "a" * 2000 + "NEEDLE" + "b" * 2000)

    snippet = memory.search("NEEDLE")[0]["snippet"]
    assert "NEEDLE" in snippet
    assert len(snippet) <= SNIPPET_LENGTH + 2  # plus the ellipses


def test_search_carries_the_session_title(memory):
    session = memory.start_session()
    memory.add_message(session, "user", "why did checkout 500?")

    assert memory.search("checkout")[0]["session_title"] == "why did checkout 500?"


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_usage_accumulates_across_turns(memory):
    session = memory.start_session()
    memory.record_usage(session, prompt_tokens=100, completion_tokens=20, cost_usd=0.01)
    memory.record_usage(session, prompt_tokens=50, completion_tokens=10, cost_usd=0.02)

    row = memory.session(session)
    assert row["prompt_tokens"] == 150
    assert row["completion_tokens"] == 30
    assert abs(row["cost_usd"] - 0.03) < 1e-9


def test_total_cost_sums_every_session(memory):
    for cost in (0.01, 0.02):
        session = memory.start_session()
        memory.record_usage(session, cost_usd=cost)

    assert abs(memory.total_cost() - 0.03) < 1e-9


def test_total_cost_is_zero_with_no_sessions(memory):
    assert memory.total_cost() == 0


# ---------------------------------------------------------------------------
# Sharing the database with the incident store
# ---------------------------------------------------------------------------


def test_chat_memory_shares_the_incident_database(tmp_path):
    """One file, one migration ladder — recall can join the two tables."""
    path = tmp_path / "incidents.db"
    store = IncidentStore(path)
    memory = ChatMemory(path)

    session = memory.start_session()
    memory.add_message(session, "user", "hello")
    assert len(memory.messages(session)) == 1
    assert store.all() == []

    memory.close()
    store.close()


def test_chat_memory_can_open_a_database_the_store_made_first(tmp_path):
    path = tmp_path / "incidents.db"
    IncidentStore(path).close()

    memory = ChatMemory(path)
    session = memory.start_session()
    memory.add_message(session, "user", "hello")
    assert len(memory.messages(session)) == 1
    memory.close()


def test_chat_memory_migrates_a_database_that_predates_chat(tmp_path):
    import sqlite3

    path = tmp_path / "incidents.db"
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

    memory = ChatMemory(path)
    session = memory.start_session()
    memory.add_message(session, "user", "hello")
    assert len(memory.messages(session)) == 1
    memory.close()


def test_sessions_persist_across_reopen(tmp_path):
    path = tmp_path / "incidents.db"
    first = ChatMemory(path)
    session = first.start_session()
    first.add_message(session, "user", "remember this")
    first.close()

    second = ChatMemory(path)
    assert second.search("remember")[0]["session_id"] == session
    second.close()
