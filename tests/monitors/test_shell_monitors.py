import logging

import pytest

from maajun.monitors import DockerLogMonitor, JournaldMonitor
from maajun.monitors.journald import cursor_path
from maajun.monitors.shell import CommandOutput, run_text

TRACEBACK = """\
ERROR django.request Internal Server Error: /api/teams/
Traceback (most recent call last):
  File "/srv/kfl/views.py", line 12, in teams
    return table[key]
KeyError: 'discount'
"""


@pytest.fixture
def stub(monkeypatch):
    """Replace run_text; returns the list of commands it was called with."""
    calls: list[list[str]] = []

    def install(*outputs: CommandOutput):
        queue = list(outputs)

        def fake(cmd, *, timeout=30.0):
            calls.append(cmd)
            return queue.pop(0) if queue else CommandOutput()

        monkeypatch.setattr("maajun.monitors.shell.run_text", fake)
        return calls

    return install


# ---------------------------------------------------------------------------
# run_text
# ---------------------------------------------------------------------------


def test_run_text_captures_output():
    result = run_text(["echo", "hello"])
    assert result.stdout.strip() == "hello"
    assert result.error == ""


def test_a_missing_binary_is_an_error_not_an_exception():
    """journalctl and docker are not installed everywhere maajun runs."""
    result = run_text(["maajun-no-such-binary-xyz"])
    assert "could not run" in result.error
    assert result.stdout == ""


def test_a_non_zero_exit_reports_the_first_line_of_stderr():
    result = run_text(["sh", "-c", "echo 'No such container: kfl' >&2; exit 1"])
    assert "exited 1" in result.error
    assert "No such container: kfl" in result.error


# ---------------------------------------------------------------------------
# journald
# ---------------------------------------------------------------------------


def test_journald_reads_a_window_until_a_cursor_exists(stub, tmp_path):
    """Reading the whole journal on first run would file every historical
    error at once, so a time window stands in until journalctl writes one."""
    calls = stub()
    monitor = JournaldMonitor("kfl.service", cursor_dir=tmp_path)

    await_poll(monitor)
    assert "--since" in calls[0]
    cursor = cursor_path(tmp_path, "kfl.service")
    assert f"--cursor-file={cursor}" in calls[0]

    cursor.write_text("s=abc")
    await_poll(monitor)
    # Both flags together would let --since skip entries the cursor has not
    # handed over yet.
    assert "--since" not in calls[1]


def test_journald_output_is_the_message_alone(stub, tmp_path):
    """-o cat: the default format prefixes each line, which un-indents a
    traceback and leaves it ungroupable."""
    calls = stub()
    monitor = JournaldMonitor("kfl.service", cursor_dir=tmp_path)
    await_poll(monitor)
    assert calls[0][calls[0].index("-o") + 1] == "cat"


def test_journald_falls_back_to_a_window_when_the_cursor_dir_is_unusable(
    stub, tmp_path, caplog
):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    calls = stub()
    with caplog.at_level(logging.WARNING):
        monitor = JournaldMonitor("kfl.service", cursor_dir=blocker / "cursors")

    assert monitor.cursor_file is None
    assert "cannot use a journal cursor" in caplog.text
    await_poll(monitor)
    assert "--since" in calls[0]
    assert not any(arg.startswith("--cursor-file") for arg in calls[0])


def test_a_unit_name_becomes_a_safe_cursor_filename(tmp_path):
    path = cursor_path(tmp_path, "web@1.service")
    assert path.name == "web@1.service.cursor"
    assert cursor_path(tmp_path, "a/b.service").parent == tmp_path


def test_journald_groups_a_traceback_into_one_event(stub, tmp_path):
    stub(CommandOutput(stdout=TRACEBACK))
    monitor = JournaldMonitor("kfl.service", cursor_dir=tmp_path)

    events = await_poll(monitor)

    assert len(events) == 1
    assert events[0].source == "journald:kfl.service"
    assert "KeyError: 'discount'" in events[0].details


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


def test_docker_reads_a_window_per_poll(stub):
    calls = stub()
    monitor = DockerLogMonitor("kfl-web-1")

    await_poll(monitor)

    assert calls[0][:3] == ["docker", "logs", "--since"]
    assert calls[0][-1] == "kfl-web-1"


def test_docker_reads_the_containers_stderr(stub):
    """An unhandled exception goes to stderr, which docker relays as its own;
    reading stdout alone would miss every traceback."""
    stub(CommandOutput(stdout="", stderr=TRACEBACK))
    monitor = DockerLogMonitor("kfl-web-1")

    events = await_poll(monitor)

    assert len(events) == 1
    assert "KeyError: 'discount'" in events[0].details


def test_a_stopped_container_yields_nothing_and_logs_once(stub, caplog):
    stub(
        CommandOutput(error="docker exited 1: No such container: kfl-web-1"),
        CommandOutput(error="docker exited 1: No such container: kfl-web-1"),
    )
    monitor = DockerLogMonitor("kfl-web-1")

    with caplog.at_level(logging.WARNING):
        assert await_poll(monitor) == []
        assert await_poll(monitor) == []

    # Every poll forever, otherwise.
    assert caplog.text.count("No such container") == 1


def test_a_failed_read_is_not_parsed_as_log_text(stub):
    """"Error response from daemon: ..." matches the error pattern, and would
    be filed as an incident against the user's code."""
    stub(CommandOutput(error="docker exited 1: ERROR: no such container"))
    monitor = DockerLogMonitor("kfl-web-1")

    assert await_poll(monitor) == []


def test_the_window_only_advances_after_a_successful_read(stub):
    """A transient failure must not skip past the logs it failed to read."""
    stub(CommandOutput(error="docker timed out after 30s"))
    monitor = DockerLogMonitor("kfl-web-1")
    before = monitor.since

    await_poll(monitor)

    assert monitor.since == before


def test_a_traceback_split_across_two_polls_is_one_event(stub):
    """The window boundary can land mid-traceback; the carry-over in the
    shared stream engine has to cover the shelled-out sources too."""
    stub(
        CommandOutput(stdout="ERROR boom\nTraceback (most recent call last):\n"),
        CommandOutput(stdout='  File "x.py", line 1\nValueError: nope\n'),
    )
    monitor = DockerLogMonitor("kfl-web-1")

    assert await_poll(monitor) == []
    events = await_poll(monitor)

    assert len(events) == 1
    assert "ValueError: nope" in events[0].details


def await_poll(monitor):
    """Run one poll. The suite is asyncio_mode=auto, but these tests are
    otherwise synchronous and read better without the await noise."""
    import asyncio

    return asyncio.run(monitor.poll())
