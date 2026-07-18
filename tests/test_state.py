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
