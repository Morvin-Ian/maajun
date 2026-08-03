from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from maajun.monitors.base import ErrorEvent
from maajun.utils import utcnow_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    message     TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'new',
    branch      TEXT,
    pr_url      TEXT,
    cost_usd    REAL DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0
)
"""

# Incident lifecycle: new -> processed, or new -> failed -> new (retried) ->
# ... -> failed permanently once MAX_ATTEMPTS is reached.

# How many times a failed incident is retried before it is left alone. Incident
# failures are usually transient (a GitHub 502, a rate limit, a dropped
# connection); a handful of retries clears those without looping forever on an
# error that genuinely cannot be processed.
MAX_ATTEMPTS = 3


class IncidentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a reader (e.g. the cost-audit query) run without blocking
        # the daemon's writes, and survives an ungraceful kill more cleanly.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns missing from a database created by an older version.

        CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a new
        column has to be added explicitly or every query naming it fails on
        an upgraded install.
        """
        present = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(incidents)").fetchall()
        }
        for column, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),):
            if column not in present:
                self._conn.execute(f"ALTER TABLE incidents ADD COLUMN {column} {ddl}")

    def record(self, event: ErrorEvent) -> bool:
        """Record a sighting. Returns True if this error should be handled.

        True for a genuinely new error, and again for one whose last attempt
        failed and still has retries left — otherwise a single transient
        GitHub 502 would blacklist that error permanently.
        """
        now = utcnow_iso()
        existing = self._conn.execute(
            "SELECT status, attempts FROM incidents WHERE fingerprint = ?",
            (event.fingerprint,),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                "INSERT INTO incidents (fingerprint, source, message, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?)",
                (event.fingerprint, event.source, event.message, now, now),
            )
            self._conn.commit()
            return True

        self._conn.execute(
            "UPDATE incidents SET last_seen = ?, count = count + 1 WHERE fingerprint = ?",
            (now, event.fingerprint),
        )
        self._conn.commit()
        retryable = (
            existing["status"] == "failed" and existing["attempts"] < MAX_ATTEMPTS
        )
        if retryable:
            log.info(
                "retrying failed incident fp=%s (attempt %d of %d)",
                event.fingerprint, existing["attempts"] + 1, MAX_ATTEMPTS,
            )
        return retryable

    def mark_processed(
        self,
        fp: str,
        *,
        branch: str,
        pr_url: str,
        cost_usd: float = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self._conn.execute(
            "UPDATE incidents SET status = 'processed', branch = ?, pr_url = ?,"
            " cost_usd = ?, prompt_tokens = ?, completion_tokens = ?"
            " WHERE fingerprint = ?",
            (branch, pr_url, cost_usd, prompt_tokens, completion_tokens, fp),
        )
        self._conn.commit()

    def total_cost(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM incidents"
        ).fetchone()
        return row["total"]

    def cost_since(self, since: str) -> float:
        """Total USD spent on incidents last seen at or after `since`.

        Backs the daily spend cap. Keyed on last_seen because that is when the
        cost was actually incurred — an old incident re-analyzed today should
        count against today.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM incidents"
            " WHERE last_seen >= ?",
            (since,),
        ).fetchone()
        return row["total"]

    def total_tokens(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) as prompt,"
            " COALESCE(SUM(completion_tokens), 0) as completion"
            " FROM incidents"
        ).fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    def forget(self, fp: str) -> None:
        """Drop an incident so a future poll treats the error as new."""
        self._conn.execute("DELETE FROM incidents WHERE fingerprint = ?", (fp,))
        self._conn.commit()

    def mark_failed(self, fp: str) -> None:
        """Mark an attempt as failed and count it toward the retry limit."""
        self._conn.execute(
            "UPDATE incidents SET status = 'failed', attempts = attempts + 1"
            " WHERE fingerprint = ?",
            (fp,),
        )
        self._conn.commit()

    def exhausted(self) -> list[dict]:
        """Incidents that failed MAX_ATTEMPTS times and are no longer retried."""
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE status = 'failed' AND attempts >= ?"
            " ORDER BY last_seen DESC",
            (MAX_ATTEMPTS,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get(self, fp: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ?", (fp,)
        ).fetchone()
        return dict(row) if row else None

    def all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM incidents ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
