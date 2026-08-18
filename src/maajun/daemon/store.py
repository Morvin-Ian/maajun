from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from maajun.monitors.base import ErrorEvent
from maajun.utils import utcnow_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint TEXT NOT NULL,
    repo        TEXT NOT NULL DEFAULT '',
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
    attempts    INTEGER NOT NULL DEFAULT 0,
    report_text TEXT NOT NULL DEFAULT '',
    artifact_kind TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (fingerprint, repo)
)
"""

# Declared in the order the table declares them, so a rebuild can copy across
# whichever subset an older database happens to have.
INCIDENT_COLUMNS: tuple[str, ...] = (
    "fingerprint", "repo", "source", "message", "first_seen", "last_seen",
    "count", "status", "branch", "pr_url", "cost_usd", "prompt_tokens",
    "completion_tokens", "attempts", "report_text", "artifact_kind",
)

# Bumped whenever a migration is appended to MIGRATIONS. Stored in the file's
# PRAGMA user_version, which starts at 0 on databases written before any of
# this existed.
SCHEMA_VERSION = 3

# What an incident produced. Recorded explicitly because it used to be
# inferred from `branch != ""`, which cannot tell a suggest-mode issue from a
# local-mode report, and breaks the moment either grows a branch.
ARTIFACT_PR = "pr"
ARTIFACT_ISSUE = "issue"
ARTIFACT_REPORT = "report"

# local mode.
NO_REPO = ""


class StoreError(RuntimeError):
    """An incident database that cannot be used as it stands."""


def _columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    """Create the incidents table, rebuilding an outdated one in place.

    Databases written before maajun tracked a schema version sit at 0 and may
    be missing columns entirely — or, from before multi-repo support, have the
    wrong primary key. Both are repaired by copying the rows across whatever
    columns the old table does have; `repo` picks up its '' default, which is
    what a single-repo install meant anyway.

    The alternative was what this replaces: refusing to open and telling the
    user to delete their incident history.
    """
    existing = _columns_of(conn, "incidents")
    if not existing:
        conn.execute(SCHEMA)
        return

    primary_key = [
        row["name"] for row in conn.execute("PRAGMA table_info(incidents)") if row["pk"]
    ]
    if set(existing) == set(INCIDENT_COLUMNS) and primary_key == ["fingerprint", "repo"]:
        return

    log.info("migrating the incidents table to the current schema")
    shared = [name for name in INCIDENT_COLUMNS if name in existing]
    columns = ", ".join(shared)
    conn.execute("ALTER TABLE incidents RENAME TO incidents_outdated")
    conn.execute(SCHEMA)
    conn.execute(
        f"INSERT INTO incidents ({columns}) SELECT {columns} FROM incidents_outdated"
    )
    conn.execute("DROP TABLE incidents_outdated")


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, name: str, ddl: str
) -> None:
    """ALTER in a column, tolerating a table that already has it.

    Needed because SCHEMA always describes the newest shape: a fresh database
    is created with every column present, so a later migration must be a
    no-op there while still upgrading an existing file.
    """
    if name in _columns_of(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """Keep the analysis text and say what the incident produced.

    Only local mode ever wrote the report anywhere maajun could read it back;
    in suggest mode the analysis existed solely in the GitHub issue. Storing
    it makes an incident answerable offline — which is what `maajun chat`
    recalls when asked about a past error.
    """
    _add_column_if_missing(conn, "incidents", "report_text", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(
        conn, "incidents", "artifact_kind", "TEXT NOT NULL DEFAULT ''"
    )
    # Backfill what the old rows can tell us: a branch means fix mode opened a
    # PR, an http(s) URL without one means an issue, anything else is a report
    # path written locally.
    conn.execute(
        "UPDATE incidents SET artifact_kind = CASE"
        "  WHEN COALESCE(branch, '') != '' THEN ?"
        "  WHEN COALESCE(pr_url, '') LIKE 'http%' THEN ?"
        "  WHEN COALESCE(pr_url, '') != '' THEN ?"
        "  ELSE '' END"
        " WHERE artifact_kind = ''",
        (ARTIFACT_PR, ARTIFACT_ISSUE, ARTIFACT_REPORT),
    )


CHAT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT '',
        cost_usd   REAL NOT NULL DEFAULT 0,
        prompt_tokens     INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS chat_messages_by_session"
    " ON chat_messages (session_id, id)",
)


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """Add chat memory: what was discussed, and what it cost.

    In the same file as the incidents so a recall query can join the two —
    "what did we decide about that KeyError?" spans both tables.
    """
    for statement in CHAT_SCHEMA:
        conn.execute(statement)


# Index i applies to a database at user_version i, taking it to i + 1.
MIGRATIONS = (_migrate_to_1, _migrate_to_2, _migrate_to_3)

# Incident lifecycle: new -> processed, or new -> failed -> new (retried) ->
# ... -> failed permanently once MAX_ATTEMPTS is reached.
MAX_ATTEMPTS = 3


def connect(path: str | Path) -> sqlite3.Connection:
    """Open maajun's database, applying any pending migrations.

    One file holds both the incident record and chat memory, so both go
    through this: a single migration ladder, and recall can join a
    conversation against the incidents it discussed.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL lets a reader (e.g. the cost-audit query, or a chat session running
    # alongside the daemon) work without blocking writes, and survives an
    # ungraceful kill more cleanly.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn, path)
    return conn


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Bring the database up to SCHEMA_VERSION, or explain why it can't be.

    Each migration runs in its own transaction and bumps user_version, so an
    interrupted upgrade resumes from the last step that committed rather than
    half-applying.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise StoreError(
            f"{path} was written by a newer version of maajun "
            f"(schema {version}, this build understands {SCHEMA_VERSION}). "
            "Upgrade maajun, or point daemon.workdir at a different directory."
        )
    for target, migration in enumerate(MIGRATIONS[version:], start=version + 1):
        try:
            with conn:
                migration(conn)
                # Not parameterizable, and `target` is a loop index over a
                # module constant — never user input.
                conn.execute(f"PRAGMA user_version = {target}")
        except sqlite3.Error as e:
            conn.close()
            raise StoreError(
                f"Could not upgrade {path} to schema {target}: {e}"
            ) from e


class IncidentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._conn = connect(self.path)

    def record(self, event: ErrorEvent) -> bool:
        """Record a sighting. Returns True if this error should be handled.

        True for a genuinely new error; for one still at 'new', meaning it was
        recorded but never published; and for one whose last attempt failed
        and still has retries left — otherwise a single transient GitHub 502
        would blacklist that error permanently.

        'new' is a real state, not just a transient one. An incident deferred
        by the spend cap stays there until the cap lifts, and one interrupted
        by a daemon that was killed mid-analysis is picked up on the next
        poll instead of being stranded.

        Scoped to `event.repo`: the same traceback in two repos is two
        incidents, because it needs two issues in two places. Deduping on the
        error text alone meant whichever repo was polled first claimed the
        error and the others were silently dropped as already known.
        """
        now = utcnow_iso()
        existing = self._conn.execute(
            "SELECT status, attempts FROM incidents"
            " WHERE fingerprint = ? AND repo = ?",
            (event.fingerprint, event.repo),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                "INSERT INTO incidents"
                " (fingerprint, repo, source, message, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event.fingerprint, event.repo, event.source, event.message, now, now),
            )
            self._conn.commit()
            return True

        self._conn.execute(
            "UPDATE incidents SET last_seen = ?, count = count + 1"
            " WHERE fingerprint = ? AND repo = ?",
            (now, event.fingerprint, event.repo),
        )
        self._conn.commit()
        if existing["status"] == "new":
            # Recorded but never published: deferred by the spend cap, or
            # interrupted before it could be marked processed or failed.
            log.debug(
                "picking up unhandled incident fp=%s repo=%s",
                event.fingerprint, event.repo or NO_REPO,
            )
            return True

        retryable = (
            existing["status"] == "failed" and existing["attempts"] < MAX_ATTEMPTS
        )
        if retryable:
            log.info(
                "retrying failed incident fp=%s repo=%s (attempt %d of %d)",
                event.fingerprint, event.repo or NO_REPO,
                existing["attempts"] + 1, MAX_ATTEMPTS,
            )
        return retryable

    def mark_processed(
        self,
        fp: str,
        repo: str = NO_REPO,
        *,
        branch: str,
        pr_url: str,
        cost_usd: float = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        report_text: str = "",
        artifact_kind: str = "",
    ) -> None:
        self._conn.execute(
            "UPDATE incidents SET status = 'processed', branch = ?, pr_url = ?,"
            " cost_usd = ?, prompt_tokens = ?, completion_tokens = ?,"
            " report_text = ?, artifact_kind = ?"
            " WHERE fingerprint = ? AND repo = ?",
            (
                branch, pr_url, cost_usd, prompt_tokens, completion_tokens,
                report_text, artifact_kind, fp, repo,
            ),
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

    def forget(self, fp: str, repo: str = NO_REPO) -> None:
        """Drop one repo's incident so a future poll treats the error as new."""
        self._conn.execute(
            "DELETE FROM incidents WHERE fingerprint = ? AND repo = ?", (fp, repo)
        )
        self._conn.commit()

    def mark_failed(self, fp: str, repo: str = NO_REPO) -> None:
        """Mark an attempt as failed and count it toward the retry limit."""
        self._conn.execute(
            "UPDATE incidents SET status = 'failed', attempts = attempts + 1"
            " WHERE fingerprint = ? AND repo = ?",
            (fp, repo),
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

    def get(self, fp: str, repo: str = NO_REPO) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ? AND repo = ?", (fp, repo)
        ).fetchone()
        return dict(row) if row else None

    def all(self, repo: str | None = None) -> list[dict]:
        """Every incident, newest sighting first; one repo's when `repo` is given."""
        if repo is None:
            rows = self._conn.execute(
                "SELECT * FROM incidents ORDER BY last_seen DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE repo = ? ORDER BY last_seen DESC",
                (repo,),
            ).fetchall()
        return [dict(r) for r in rows]

    def repos(self) -> list[str]:
        """Distinct repos that have incidents, so a caller can offer a filter."""
        rows = self._conn.execute(
            "SELECT DISTINCT repo FROM incidents ORDER BY repo"
        ).fetchall()
        return [row["repo"] for row in rows]

    def close(self) -> None:
        self._conn.close()
