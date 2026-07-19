from __future__ import annotations

import sqlite3
from pathlib import Path

from maajun.monitors.base import ErrorEvent
from maajun.utils import utcnow_iso

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
    completion_tokens INTEGER DEFAULT 0
)
"""

# Incident lifecycle: new -> processed | failed


class IncidentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def record(self, event: ErrorEvent) -> bool:
        """Record a sighting. Returns True if this error is new."""
        now = utcnow_iso()
        cur = self._conn.execute(
            "UPDATE incidents SET last_seen = ?, count = count + 1 WHERE fingerprint = ?",
            (now, event.fingerprint),
        )
        if cur.rowcount:
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO incidents (fingerprint, source, message, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            (event.fingerprint, event.source, event.message, now, now),
        )
        self._conn.commit()
        return True

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
        self._conn.execute(
            "UPDATE incidents SET status = 'failed' WHERE fingerprint = ?", (fp,)
        )
        self._conn.commit()

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
