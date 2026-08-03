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
        f.write("ERROR first\nINFO ok\n")
    assert len(await monitor.poll()) == 1
    assert await monitor.poll() == []

    with open(logfile, "a") as f:
        f.write("ERROR second\nINFO ok\n")
    events = await monitor.poll()
    assert len(events) == 1
    assert "second" in events[0].message


async def test_trailing_error_line_flushes_on_quiet_poll(logfile):
    monitor = LogFileMonitor(logfile)
    with open(logfile, "a") as f:
        f.write("ERROR at end of file\n")

    # Held back one poll in case a traceback follows it...
    assert await monitor.poll() == []
    # ...then flushed once the file stays quiet.
    events = await monitor.poll()
    assert len(events) == 1
    assert "at end of file" in events[0].message
    assert await monitor.poll() == []


async def test_logging_exception_yields_single_merged_event(logfile):
    """An ERROR line immediately followed by a traceback is one incident."""
    monitor = LogFileMonitor(logfile)
    with open(logfile, "a") as f:
        f.write("2026-07-18 ERROR shop: failed to process order 3\n")
        f.write(TRACEBACK)
        f.write("INFO next\n")

    events = await monitor.poll()
    assert len(events) == 1
    assert events[0].message == "IndexError: list index out of range"
    assert "failed to process order 3" in events[0].details
    assert "Traceback" in events[0].details


async def test_handles_truncation(logfile):
    monitor = LogFileMonitor(logfile)
    with open(logfile, "a") as f:
        f.write("ERROR before rotation\nINFO ok\n")
    assert len(await monitor.poll()) == 1

    logfile.write_text("ERROR after rotation\nINFO ok\n")
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


# -- detection improvements ----------------------------------------------


async def test_warnings_are_ignored_by_default(logfile):
    """Warnings are noise by default: every match costs an AI call and a PR."""
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("2026-07-18 WARNING disk space low\n")
        f.write("2026-07-18 WARN connection timeout\n")
        f.write("2026-07-18 INFO all good\n")

    assert await monitor.poll() == []


async def test_warnings_detected_when_pattern_opts_in(logfile):
    monitor = LogFileMonitor(
        logfile, error_pattern=r"\b(ERROR|CRITICAL|FATAL|WARNING|WARN)\b"
    )
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("2026-07-18 WARNING disk space low\n")
        f.write("2026-07-18 INFO all good\n")

    events = await monitor.poll()
    assert len(events) == 1
    assert "disk space low" in events[0].message


async def test_detects_json_error_level(logfile):
    monitor = LogFileMonitor(logfile, json_level_field="level")
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write('{"level": "error", "message": "db timeout"}\n')
        f.write('{"level": "info", "message": "all good"}\n')

    events = await monitor.poll()
    assert len(events) == 1
    assert "db timeout" in events[0].message


async def test_detects_json_severity_field(logfile):
    monitor = LogFileMonitor(logfile, json_level_field="severity")
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write('{"severity": "critical", "msg": "high memory"}\n')

    events = await monitor.poll()
    assert len(events) == 1
    assert "high memory" in events[0].message


async def test_json_non_matching_level_is_skipped(logfile):
    monitor = LogFileMonitor(logfile, json_level_field="level")
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write('{"level": "debug", "message": "verbose"}\n')
        f.write('{"level": "info", "message": "ok"}\n')

    assert await monitor.poll() == []


async def test_json_with_custom_level_values(logfile):
    monitor = LogFileMonitor(
        logfile,
        json_level_field="level",
        json_level_values=frozenset({"fatal", "emergency"}),
    )
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write('{"level": "error", "message": "not reported"}\n')
        f.write('{"level": "fatal", "message": "reported"}\n')

    events = await monitor.poll()
    assert len(events) == 1
    assert "reported" in events[0].message


async def test_detects_java_traceback(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    java_tb = (
        'Exception in thread "main" java.lang.NullPointerException\n'
        "\tat com.example.MyClass.method(MyClass.java:42)\n"
        "\tat com.example.Main.main(Main.java:10)\n"
    )
    with open(logfile, "a") as f:
        f.write(java_tb)

    assert await monitor.poll() == []
    events = await monitor.poll()
    assert len(events) == 1
    assert "java.lang.NullPointerException" in events[0].message
    assert "MyClass.java:42" in events[0].details


async def test_detects_go_panic(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    go_tb = (
        "panic: runtime error: invalid memory address\n"
        "\n"
        "goroutine 1 [running]:\n"
        "main.main()\n"
        "\t/home/user/main.go:10 +0x39\n"
    )
    with open(logfile, "a") as f:
        f.write(go_tb)

    assert await monitor.poll() == []
    events = await monitor.poll()
    assert len(events) == 1
    assert "invalid memory address" in events[0].message


async def test_detects_caused_by_chain(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    java_chain = (
        "ERROR processing order 42\n"
        "com.example.OrderException: Invalid status\n"
        "\tat com.example.OrderService.process(OrderService.java:55)\n"
        "Caused by: com.example.ValidationException: missing field\n"
        "\tat com.example.Validator.check(Validator.java:22)\n"
    )
    with open(logfile, "a") as f:
        f.write(java_chain)

    assert await monitor.poll() == []
    events = await monitor.poll()
    assert len(events) == 1
    assert "missing field" in events[0].message


# -- burst thresholding --------------------------------------------------


async def test_burst_threshold_holds_events_below_threshold(logfile):
    monitor = LogFileMonitor(logfile, burst_threshold=3, burst_window_seconds=60)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR first\nERROR second\nINFO end\n")

    assert await monitor.poll() == []


async def test_burst_threshold_emits_whole_burst_when_reached(logfile):
    monitor = LogFileMonitor(logfile, burst_threshold=3, burst_window_seconds=60)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR first\nERROR second\nERROR third\nINFO end\n")

    events = await monitor.poll()
    assert len(events) == 3


async def test_burst_buffer_resets_after_emit(logfile):
    monitor = LogFileMonitor(logfile, burst_threshold=2, burst_window_seconds=60)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR first\nERROR second\nINFO end\n")

    assert len(await monitor.poll()) == 2

    with open(logfile, "a") as f:
        f.write("ERROR third\nINFO end\n")

    assert await monitor.poll() == []  # buffer was cleared


async def test_burst_threshold_default_emits_immediately(logfile):
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR first\nINFO end\n")

    events = await monitor.poll()
    assert len(events) == 1  # immediate emit


async def test_burst_threshold_keeps_events_buffered_across_polls(logfile):
    """The whole burst is emitted, not just the batch that crossed the line.

    Regression: the buffer was drained into a discarded local and the caller
    returned only the current poll's events, silently losing everything held
    from earlier polls.
    """
    monitor = LogFileMonitor(logfile, burst_threshold=3, burst_window_seconds=60)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR one\nERROR two\nINFO end\n")
    assert await monitor.poll() == []

    with open(logfile, "a") as f:
        f.write("ERROR three\nINFO end\n")

    events = await monitor.poll()
    assert [e.message for e in events] == ["ERROR one", "ERROR two", "ERROR three"]


async def test_flush_emits_incomplete_burst(logfile):
    """--once must not discard a burst that never reached its threshold."""
    monitor = LogFileMonitor(logfile, burst_threshold=5, burst_window_seconds=60)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR one\nERROR two\nINFO end\n")
    assert await monitor.poll() == []

    events = await monitor.flush()
    assert [e.message for e in events] == ["ERROR one", "ERROR two"]


async def test_error_lines_are_not_swallowed_by_a_distant_traceback(logfile):
    """A traceback far below an error line belongs to a different failure.

    Regression: the lookahead scanned to the end of the buffer, merging
    unrelated events and consuming every line in between as "context".
    """
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR first failure\n")
        f.writelines("INFO noise\n" for _ in range(5))
        f.write("ERROR second failure\n")
        f.write(TRACEBACK)
        f.write("INFO done\n")

    events = await monitor.poll()
    messages = [e.message for e in events]
    assert messages == ["ERROR first failure", "IndexError: list index out of range"]
    # The nearby traceback still merges into the error it belongs to.
    assert "second failure" in events[1].details
    assert "first failure" not in events[1].details


async def test_traceback_within_lookahead_still_merges(logfile):
    """The logging.exception pattern survives an intervening detail line."""
    monitor = LogFileMonitor(logfile)
    await monitor.poll()

    with open(logfile, "a") as f:
        f.write("ERROR shop: failed to process order 3\n")
        f.write("  context: user_id=42\n")
        f.write(TRACEBACK)
        f.write("INFO next\n")

    events = await monitor.poll()
    assert len(events) == 1
    assert events[0].message == "IndexError: list index out of range"
    assert "user_id=42" in events[0].details
