import asyncio

import pytest

from maajun.monitors import ErrorEvent, LogFileMonitor, fingerprint
from maajun.monitors.cursors import read_position

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


# ---------------------------------------------------------------------------
# Where a traceback ends
# ---------------------------------------------------------------------------


JAVA_THEN_IDLE = """\
Exception in thread "main" java.lang.NullPointerException
\tat com.example.Foo.bar(Foo.java:10)
\tat com.example.Foo.main(Foo.java:5)

2026-08-21 10:00:00 INFO server ready on :8080
"""


async def test_a_blank_line_ends_a_trace_before_the_next_log_line(logfile):
    """Blank lines used to be swallowed and the following unindented line
    taken as the exception, so an unrelated INFO line ended up quoted in the
    incident — and, for a header that is not self-describing, became its
    title."""
    monitor = LogFileMonitor(logfile)
    await monitor.poll()
    with open(logfile, "a") as f:
        f.write(JAVA_THEN_IDLE)

    events = await monitor.poll()

    assert len(events) == 1
    assert "server ready" not in events[0].details
    assert "Foo.java:10" in events[0].details


async def test_the_line_after_a_trace_does_not_change_its_fingerprint(logfile):
    """Absorbing it meant the same failure fingerprinted differently
    depending on what the application happened to log next — two incidents,
    two issues, for one error."""
    monitor = LogFileMonitor(logfile)
    await monitor.poll()
    with open(logfile, "a") as f:
        f.write(JAVA_THEN_IDLE)
    first = (await monitor.poll())[0]

    other = logfile.parent / "other.log"
    other.write_text("")
    monitor2 = LogFileMonitor(other)
    await monitor2.poll()
    with open(other, "a") as f:
        f.write(JAVA_THEN_IDLE.replace("server ready on :8080", "cache warmed"))
    second = (await monitor2.poll())[0]

    assert first.fingerprint == second.fingerprint


async def test_a_blank_line_inside_a_chained_trace_is_kept(logfile):
    """"During handling of the above" continues the trace; the blank line
    before it is part of it, not the end of it."""
    monitor = LogFileMonitor(logfile)
    await monitor.poll()
    with open(logfile, "a") as f:
        f.write(
            "Exception in thread \"main\" java.lang.IllegalStateException\n"
            "\tat com.example.A.run(A.java:3)\n"
            "\n"
            "Caused by: java.io.IOException: disk full\n"
            "\tat com.example.B.write(B.java:9)\n"
            "\n"
            "2026-08-21 10:00:01 INFO idle\n"
        )

    events = await monitor.poll()

    assert len(events) == 1
    assert "disk full" in events[0].details
    assert "idle" not in events[0].details


# ---------------------------------------------------------------------------
# Reading bytes, not the host's locale
# ---------------------------------------------------------------------------


READS_UTF8 = """\
import asyncio, pathlib, sys
from maajun.monitors import LogFileMonitor

path = pathlib.Path(sys.argv[1])
events = asyncio.run(LogFileMonitor(path, backfill=True).poll())
sys.stdout.buffer.write(events[0].message.encode("utf-8"))
"""


def test_a_log_is_decoded_as_utf8_whatever_the_host_locale_is(tmp_path):
    """Text mode picks the *locale* encoding. A container defaulting to the C
    locale therefore turned every non-ASCII byte in the log into U+FFFD, and
    the mangled text is what went to the model and into the issue.

    Run in a subprocess because open()'s encoding is resolved in C, below
    anything a monkeypatch can reach.
    """
    import subprocess
    import sys

    log = tmp_path / "app.log"
    log.write_bytes("ERROR café lookup failed for Ünal\nINFO idle\n".encode())
    script = tmp_path / "read.py"
    script.write_text(READS_UTF8)

    proc = subprocess.run(
        [sys.executable, str(script), str(log)],
        capture_output=True,
        env={"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PATH": "/usr/bin:/bin"},
        check=True,
    )

    assert proc.stdout.decode("utf-8") == "ERROR café lookup failed for Ünal"


def test_the_offset_stays_comparable_with_the_file_size(tmp_path):
    """read_new decides a file was truncated by comparing its offset against
    st_size, so the offset has to be a byte count. TextIOWrapper.tell()
    returns an opaque cookie that only usually happens to be one."""
    log = tmp_path / "app.log"
    log.write_bytes("ERROR café ☕ Ünal\n".encode())
    monitor = LogFileMonitor(log, backfill=True)

    monitor.read_new()

    assert monitor.offset == log.stat().st_size


# ---------------------------------------------------------------------------
# Where reading starts
# ---------------------------------------------------------------------------


BACKLOG = (
    'ERROR old failure\nTraceback (most recent call last):\n'
    '  File "old.py", line 1\nKeyError: "gone"\n'
)

FRESH = (
    'ERROR new failure\nTraceback (most recent call last):\n'
    '  File "new.py", line 1\nValueError: here\n'
)


def test_what_is_already_in_the_log_is_left_alone(tmp_path):
    """Starting a monitor is not a request to file an issue for every error
    of the last six months."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG * 3)

    monitor = LogFileMonitor(log)

    assert asyncio.run(monitor.poll()) == []


def test_errors_after_the_start_are_read(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    monitor = LogFileMonitor(log)

    with open(log, "a") as f:
        f.write(FRESH)
    events = asyncio.run(monitor.poll())

    assert [e.message for e in events] == ['ValueError: here']


def test_backfill_reads_the_whole_log(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(BACKLOG * 3)

    events = asyncio.run(LogFileMonitor(log, backfill=True).poll())

    assert len(events) == 3


def test_a_log_written_between_construction_and_the_first_poll_is_read(tmp_path):
    """"From now on" is measured when maajun is asked to watch, not whenever
    the first poll happens to land."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    monitor = LogFileMonitor(log)
    with open(log, "a") as f:
        f.write(FRESH)

    assert len(asyncio.run(monitor.poll())) == 1


def test_a_log_that_appears_later_is_read_whole(tmp_path):
    """Nothing in it predates the watch: the app created it after we started."""
    log = tmp_path / "app.log"
    monitor = LogFileMonitor(log)

    log.write_text(BACKLOG)

    assert len(asyncio.run(monitor.poll())) == 1


def test_a_restart_carries_on_from_the_cursor(tmp_path):
    """Without this a restart re-reads the file — cheap for a small log, and
    a fresh parse of gigabytes for a real one."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    cursors = tmp_path / "cursors"
    first = LogFileMonitor(log, cursor_dir=cursors, backfill=True)
    assert len(asyncio.run(first.poll())) == 1

    restarted = LogFileMonitor(log, cursor_dir=cursors)
    assert asyncio.run(restarted.poll()) == []

    with open(log, "a") as f:
        f.write(FRESH)
    assert len(asyncio.run(restarted.poll())) == 1


def test_a_cursor_is_written_even_when_the_log_is_quiet(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    cursors = tmp_path / "cursors"

    monitor = LogFileMonitor(log, cursor_dir=cursors)
    asyncio.run(monitor.poll())

    saved = read_position(monitor.cursor_file)
    assert saved.offset == log.stat().st_size


def test_a_log_rotated_while_the_daemon_was_down_is_read_whole(tmp_path):
    """The cursor points into a file that no longer exists, so everything in
    the new one is unread."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    cursors = tmp_path / "cursors"
    asyncio.run(LogFileMonitor(log, cursor_dir=cursors, backfill=True).poll())

    # How logrotate does it: the replacement is written elsewhere and moved
    # into place, so the path points at a different inode.
    replacement = tmp_path / "app.log.new"
    replacement.write_text(FRESH * 2)
    replacement.rename(log)
    assert log.stat().st_ino != read_position(
        LogFileMonitor(log, cursor_dir=cursors).cursor_file
    ).inode

    events = asyncio.run(LogFileMonitor(log, cursor_dir=cursors).poll())

    assert len(events) == 2


def test_a_cursor_past_the_end_is_ignored(tmp_path):
    """Truncated in place while we were down: the offset says more was read
    than the file now holds."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG * 4)
    cursors = tmp_path / "cursors"
    monitor = LogFileMonitor(log, cursor_dir=cursors, backfill=True)
    asyncio.run(monitor.poll())

    log.write_text(FRESH)  # same inode, much shorter

    assert len(asyncio.run(LogFileMonitor(log, cursor_dir=cursors).poll())) == 1


def test_a_garbled_cursor_is_ignored(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    cursors = tmp_path / "cursors"
    cursors.mkdir()
    monitor = LogFileMonitor(log, cursor_dir=cursors)
    monitor.cursor_file.write_text("not a position")

    assert asyncio.run(monitor.poll()) == []  # falls back to the default


def test_no_cursor_directory_still_monitors(tmp_path):
    """A workdir that cannot be written is not a reason to stop watching."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    monitor = LogFileMonitor(log, cursor_dir=blocker / "cursors", backfill=True)

    assert monitor.cursor_file is None
    assert len(asyncio.run(monitor.poll())) == 1


def test_backfill_overrides_a_saved_cursor(tmp_path):
    """After one ordinary run the saved position is the end of the file, so a
    backfill that respected it would read nothing — which is exactly when
    someone asks for one."""
    log = tmp_path / "app.log"
    log.write_text(BACKLOG * 2)
    cursors = tmp_path / "cursors"
    asyncio.run(LogFileMonitor(log, cursor_dir=cursors).poll())  # writes the cursor

    events = asyncio.run(
        LogFileMonitor(log, cursor_dir=cursors, backfill=True).poll()
    )

    assert len(events) == 2
