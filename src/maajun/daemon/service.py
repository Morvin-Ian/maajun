from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

STOP_TIMEOUT = 30.0
POLL = 0.2

# Rotated at startup past this size: one file written for months on a VPS
# fills the disk, and nobody is watching it to notice.
MAX_LOG_BYTES = 5 * 1024 * 1024


@dataclass
class Running:
    """A daemon that is currently up."""

    pid: int
    log_file: Path


def state_dir(workdir: str | Path) -> Path:
    return Path(workdir).expanduser()


def pid_file(workdir: str | Path) -> Path:
    return state_dir(workdir) / "watch.pid"


def log_file(workdir: str | Path) -> Path:
    return state_dir(workdir) / "watch.log"


def alive(pid: int) -> bool:
    """Whether a process exists and we may signal it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running(workdir: str | Path) -> Running | None:
    """The daemon started from this workdir, or None.

    A stale pid file is cleaned up rather than reported: a machine that lost
    power mid-run would otherwise refuse to start again.
    """
    path = pid_file(workdir)
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if not alive(pid):
        path.unlink(missing_ok=True)
        return None
    return Running(pid=pid, log_file=log_file(workdir))


def write_pid(workdir: str | Path, pid: int) -> None:
    path = pid_file(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n")


def clear_pid(workdir: str | Path) -> None:
    pid_file(workdir).unlink(missing_ok=True)


def rotate(path: Path) -> None:
    """Keep one previous log, so a long-running daemon cannot fill the disk."""
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def start(workdir: str | Path, args: list[str]) -> Running:
    """Launch `maajun watch` detached, and return the process it started.

    A new session, so closing the terminal or ending an SSH login does not
    take the daemon with it. Output goes to the log file because there is no
    terminal to write to any more.
    """
    directory = state_dir(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = log_file(workdir)
    rotate(destination)
    handle = open(destination, "a", buffering=1)
    try:
        handle.write(f"\n--- maajun watch started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        process = subprocess.Popen(
            [sys.executable, "-m", "maajun.cli", "watch", "--foreground", *args],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )
    finally:
        handle.close()
    write_pid(workdir, process.pid)
    return Running(pid=process.pid, log_file=destination)


def stop(workdir: str | Path, *, timeout: float = STOP_TIMEOUT) -> int | None:
    """Ask the daemon to finish the incident it is on, then exit.

    SIGTERM, which the loop already handles gracefully — a kill mid-push
    could leave a half-created branch. Returns the pid stopped, or None.
    """
    current = running(workdir)
    if current is None:
        return None
    try:
        os.kill(current.pid, signal.SIGTERM)
    except OSError:
        clear_pid(workdir)
        return current.pid
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(current.pid):
            break
        time.sleep(POLL)
    clear_pid(workdir)
    return current.pid


def tail(path: Path, lines: int = 20) -> str:
    """The last few lines of the daemon's log, for `maajun watch --status`."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
