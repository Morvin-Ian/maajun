import os
import subprocess
import sys
import time

from maajun.daemon import service


def spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_nothing_is_running_in_a_fresh_workdir(tmp_path):
    assert service.running(tmp_path) is None


def test_a_recorded_process_is_reported(tmp_path):
    process = spawn_sleeper()
    try:
        service.write_pid(tmp_path, process.pid)

        current = service.running(tmp_path)

        assert current is not None
        assert current.pid == process.pid
        assert current.log_file == tmp_path / "watch.log"
    finally:
        process.kill()
        process.wait()


def test_a_stale_pid_file_is_cleaned_up_not_reported(tmp_path):
    """A machine that lost power mid-run would otherwise never start again."""
    process = spawn_sleeper()
    process.kill()
    process.wait()
    service.write_pid(tmp_path, process.pid)

    assert service.running(tmp_path) is None
    assert not service.pid_file(tmp_path).exists()


def test_a_garbled_pid_file_is_ignored(tmp_path):
    service.pid_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    service.pid_file(tmp_path).write_text("not a pid")

    assert service.running(tmp_path) is None


def test_stopping_ends_the_process_and_clears_the_file(tmp_path):
    process = spawn_sleeper()
    service.write_pid(tmp_path, process.pid)
    try:
        stopped = service.stop(tmp_path, timeout=10)

        assert stopped == process.pid
        assert not service.pid_file(tmp_path).exists()
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_stopping_asks_politely(tmp_path):
    """SIGTERM, which the loop handles gracefully — a kill mid-push could
    leave a half-created branch."""
    script = "import signal, sys, time\n" \
             "signal.signal(signal.SIGTERM, lambda *a: sys.exit(7))\n" \
             "time.sleep(30)\n"
    process = subprocess.Popen([sys.executable, "-c", script])
    time.sleep(0.5)
    service.write_pid(tmp_path, process.pid)

    service.stop(tmp_path, timeout=10)

    assert process.wait(timeout=5) == 7


def test_stopping_nothing_says_so(tmp_path):
    assert service.stop(tmp_path) is None


def test_starting_detaches_and_records_the_pid(tmp_path, monkeypatch):
    """A new session, so closing the terminal does not take the daemon."""
    launched = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    started = service.start(tmp_path, ["--config", "/tmp/c.toml"])

    assert started.pid == 4321
    assert launched["kwargs"]["start_new_session"] is True
    assert launched["cmd"][1:] == [
        "-m", "maajun.cli", "watch", "--foreground", "--config", "/tmp/c.toml",
    ]
    assert service.pid_file(tmp_path).read_text().strip() == "4321"


def test_the_log_records_each_start(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 99

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: FakeProcess())

    service.start(tmp_path, [])
    service.start(tmp_path, [])

    assert service.log_file(tmp_path).read_text().count("maajun watch started") == 2


def test_tail_reads_the_end_of_the_log(tmp_path):
    service.log_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    service.log_file(tmp_path).write_text("\n".join(str(n) for n in range(100)))

    assert service.tail(service.log_file(tmp_path), lines=3) == "97\n98\n99"


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert service.tail(tmp_path / "nope.log") == ""


def test_a_process_we_may_not_signal_still_counts_as_alive():
    """PID 1 is not ours to signal, and it is certainly running."""
    assert service.alive(1) is True
    assert service.alive(os.getpid()) is True


def test_a_big_log_is_rotated_at_startup(tmp_path, monkeypatch):
    """Months of a daemon writing one file would fill the disk unnoticed."""
    log = service.log_file(tmp_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * (service.MAX_LOG_BYTES + 1))

    class FakeProcess:
        pid = 7

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: FakeProcess())
    service.start(tmp_path, [])

    assert log.with_suffix(".log.1").exists()
    assert log.stat().st_size < service.MAX_LOG_BYTES


def test_a_small_log_is_left_alone(tmp_path, monkeypatch):
    log = service.log_file(tmp_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("recent output\n")

    class FakeProcess:
        pid = 7

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: FakeProcess())
    service.start(tmp_path, [])

    assert "recent output" in log.read_text()
    assert not log.with_suffix(".log.1").exists()
