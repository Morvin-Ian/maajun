"""The monitoring daemon: detect, dedup, analyze, publish, record.

`build_daemon` is the entry point. `core` holds the poll loop and the
incident-handling flow; the rest supports it — `store` records incidents and
their cost, `reports` renders the artifacts, `prompts` holds the templates,
`wiring` turns config plus credentials into a runnable Daemon.
"""

from maajun.daemon.core import SHUTDOWN_EVENT, Daemon, LocalWorkspace
from maajun.daemon.store import IncidentStore
from maajun.daemon.wiring import build_daemon, build_daemon_for_report

__all__ = [
    "SHUTDOWN_EVENT",
    "Daemon",
    "IncidentStore",
    "LocalWorkspace",
    "build_daemon",
    "build_daemon_for_report",
]
