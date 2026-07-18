"""Tests for error monitors and fingerprinting."""

import pytest

from maajun.monitors import ErrorEvent, LogFileMonitor, fingerprint

TRACEBACK = """\
Traceback (most recent call last):
  File "/app/main.py", line 42, in handler
    result = items[index]
IndexError: list index out of range
"""


@pytest.fixture
def logfile(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("")
    return f


async def test_detects_python_traceback(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("INFO starting up\n")
        f.write(TRACEBACK)

    events = await monitor.poll()
    assert len(events) == 1
    assert events[0].message == "IndexError: list index out of range"
    assert "Traceback" in events[0].details
    assert events[0].source == f"logfile:{logfile}"


async def test_detects_error_lines(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("2026-07-18 ERROR database connection refused\n")
        f.write("2026-07-18 INFO all good\n")

    events = await monitor.poll()
    assert len(events) == 1
    assert "database connection refused" in events[0].message


async def test_only_new_content_is_read(logfile):
    monitor = LogFileMonitor(logfile)
    with open(logfile, "a") as f:
        f.write("ERROR first\n")
    assert len(await monitor.poll()) == 1
    assert await monitor.poll() == []

    with open(logfile, "a") as f:
        f.write("ERROR second\n")
    events = await monitor.poll()
    assert len(events) == 1
    assert "second" in events[0].message


async def test_handles_truncation(logfile):
    monitor = LogFileMonitor(logfile)
    with open(logfile, "a") as f:
        f.write("ERROR before rotation\n")
    await monitor.poll()

    logfile.write_text("ERROR after rotation\n")
    events = await monitor.poll()
    assert len(events) == 1
    assert "after rotation" in events[0].message


async def test_partial_traceback_carries_over(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    lines = TRACEBACK.splitlines(keepends=True)
    with open(logfile, "a") as f:
        f.writelines(lines[:2])  # header + first frame only
    assert await monitor.poll() == []

    with open(logfile, "a") as f:
        f.writelines(lines[2:])
        f.write("INFO next line\n")
    events = await monitor.poll()
    assert len(events) == 1
    assert events[0].message == "IndexError: list index out of range"


async def test_missing_file_is_quiet(tmp_path):
    monitor = LogFileMonitor(tmp_path / "does-not-exist.log")
    assert await monitor.poll() == []


def test_fingerprint_ignores_line_numbers_and_addresses():
    a = 'File "/app/main.py", line 42, in handler at 0xdeadbeef'
    b = 'File "/app/main.py", line 97, in handler at 0xcafebabe'
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_for_different_errors():
    assert fingerprint("IndexError: list index out of range") != fingerprint(
        "KeyError: 'user_id'"
    )


def test_event_gets_fingerprint_automatically():
    event = ErrorEvent(source="test", message="boom", details="ValueError: boom")
    assert event.fingerprint
    assert event.timestamp
