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

# Bumped with every MIGRATIONS entry. Databases predating this sit at 0.
SCHEMA_VERSION = 4

# What an incident produced. Recorded, not inferred from `branch != ""`,
# which cannot tell a suggest-mode issue from a local-mode report.
ARTIFACT_PR = "pr"
ARTIFACT_ISSUE = "issue"
ARTIFACT_REPORT = "report"

NO_REPO = ""  # local mode


class StoreError(RuntimeError):
    """An incident database that cannot be used as it stands."""


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def migrate_to_1(conn: sqlite3.Connection) -> None:
    """Create the incidents table, rebuilding an outdated one in place.

    Databases written before maajun tracked a schema version sit at 0 and may
    be missing columns entirely — or, from before multi-repo support, have the
    wrong primary key. Both are repaired by copying the rows across whatever
    columns the old table does have; `repo` picks up its '' default, which is
    what a single-repo install meant anyway.

    The alternative was what this replaces: refusing to open and telling the
    user to delete their incident history.
    """
    existing = columns_of(conn, "incidents")
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


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, name: str, ddl: str
) -> None:
    """ALTER in a column, tolerating a table that already has it.

    Needed because SCHEMA always describes the newest shape: a fresh database
    is created with every column present, so a later migration must be a
    no-op there while still upgrading an existing file.
    """
    if name in columns_of(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate_to_2(conn: sqlite3.Connection) -> None:
    """Keep the analysis text and say what the incident produced.

    Only local mode ever wrote the report anywhere maajun could read it back;
    in suggest mode the analysis existed solely in the GitHub issue. Storing
    it makes an incident answerable offline — which is what `maajun chat`
    recalls when asked about a past error.
    """
    add_column_if_missing(conn, "incidents", "report_text", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(
        conn, "incidents", "artifact_kind", "TEXT NOT NULL DEFAULT ''"
    )
    # Backfill: a branch means a PR, an http URL an issue, else a report.
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


def migrate_to_3(conn: sqlite3.Connection) -> None:
    """Add chat memory: what was discussed, and what it cost.

    In the same file as the incidents so a recall query can join the two —
    "what did we decide about that KeyError?" spans both tables.
    """
    for statement in CHAT_SCHEMA:
        conn.execute(statement)


FTS_SCHEMA = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
        content,
        content='chat_messages',
        content_rowid='id',
        tokenize='unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chat_messages_ai AFTER INSERT ON chat_messages
    BEGIN
        INSERT INTO chat_messages_fts (rowid, content)
        VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chat_messages_ad AFTER DELETE ON chat_messages
    BEGIN
        INSERT INTO chat_messages_fts (chat_messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chat_messages_au AFTER UPDATE ON chat_messages
    BEGIN
        INSERT INTO chat_messages_fts (chat_messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        INSERT INTO chat_messages_fts (rowid, content)
        VALUES (new.id, new.content);
    END
    """,
    "INSERT INTO chat_messages_fts (chat_messages_fts) VALUES ('rebuild')",
)


def has_fts(conn: sqlite3.Connection) -> bool:
    """Whether the full-text index exists in this file."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("chat_messages_fts",),
    ).fetchone()
    return row is not None


def ensure_fts(conn: sqlite3.Connection) -> bool:
    """Create the full-text index if this file has none. Says whether it does.

    Probed on every open, not just during migration: migrate_to_4 tolerates a
    SQLite without FTS5, and bumping user_version past it would mark the file
    current with no index and no way back.

    Runs no transaction of its own — migrate() already holds one.
    """
    if has_fts(conn):
        return True
    try:
        for statement in FTS_SCHEMA:
            conn.execute(statement)
    except sqlite3.OperationalError as e:
        log.debug("full-text search is unavailable (%s); searches will use LIKE", e)
        return False
    return True


def migrate_to_4(conn: sqlite3.Connection) -> None:
    """Index chat messages for full-text search.

    LIKE '%…%' only ever matched a query that appeared verbatim and
    contiguously, so 'checkout KeyError' found nothing — which is exactly how
    someone refers to a past conversation. Skipped without failing the upgrade
    when SQLite was built without FTS5; the searches fall back to LIKE, and
    ensure_fts picks the index up on a later open if that ever changes.
    """
    if not ensure_fts(conn):
        log.warning(
            "SQLite has no FTS5 here, so chat search will use LIKE. It will "
            "be indexed automatically if maajun later runs on a build that "
            "supports it."
        )


# Index i applies to a database at user_version i, taking it to i + 1.
MIGRATIONS = (migrate_to_1, migrate_to_2, migrate_to_3, migrate_to_4)

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
    # WAL lets a chat session read alongside the daemon's writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn, path)
    # After migrate, which creates the table the index shadows. Repairs a
    # database upgraded on a build without FTS5.
    conn.execute("BEGIN")
    if ensure_fts(conn):
        conn.commit()
    else:
        conn.rollback()
    return conn


def migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Bring the database up to SCHEMA_VERSION, or explain why it can't be.

    Each migration runs in its own transaction and bumps user_version, so an
    interrupted upgrade resumes rather than half-applies.

    The BEGIN is explicit, not `with conn`: sqlite3's legacy transaction
    control opens one before DML but not before DDL, so CREATE/ALTER/DROP
    would each autocommit and `with conn` would commit nothing that mattered.
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
            conn.execute("BEGIN")
            migration(conn)
            # Not parameterizable; `target` indexes a module constant.
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            conn.close()
            raise StoreError(
                f"Could not upgrade {path} to schema {target}: {e}"
            ) from e


class IncidentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.conn = connect(self.path)

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
        existing = self.conn.execute(
            "SELECT status, attempts FROM incidents"
            " WHERE fingerprint = ? AND repo = ?",
            (event.fingerprint, event.repo),
        ).fetchone()

        if existing is None:
            self.conn.execute(
                "INSERT INTO incidents"
                " (fingerprint, repo, source, message, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event.fingerprint, event.repo, event.source, event.message, now, now),
            )
            self.conn.commit()
            return True

        self.conn.execute(
            "UPDATE incidents SET last_seen = ?, count = count + 1"
            " WHERE fingerprint = ? AND repo = ?",
            (now, event.fingerprint, event.repo),
        )
        self.conn.commit()
        if existing["status"] == "new":
            # Deferred by the spend cap, or interrupted mid-analysis.
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
        self.conn.execute(
            "UPDATE incidents SET status = 'processed', branch = ?, pr_url = ?,"
            " cost_usd = ?, prompt_tokens = ?, completion_tokens = ?,"
            " report_text = ?, artifact_kind = ?"
            " WHERE fingerprint = ? AND repo = ?",
            (
                branch, pr_url, cost_usd, prompt_tokens, completion_tokens,
                report_text, artifact_kind, fp, repo,
            ),
        )
        self.conn.commit()

    def add_spend(
        self,
        fp: str,
        repo: str = NO_REPO,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Accumulate what an abandoned attempt already cost.

        mark_processed *sets* the totals for an incident that finished. This
        adds to them instead, for the analysis that died partway: every tool
        round was a billed request, and the daily cap reads these numbers, so
        a turn that failed on round thirty must not look free.
        """
        if not (prompt_tokens or completion_tokens or cost_usd):
            return
        self.conn.execute(
            "UPDATE incidents SET"
            " cost_usd = COALESCE(cost_usd, 0) + ?,"
            " prompt_tokens = COALESCE(prompt_tokens, 0) + ?,"
            " completion_tokens = COALESCE(completion_tokens, 0) + ?"
            " WHERE fingerprint = ? AND repo = ?",
            (cost_usd, prompt_tokens, completion_tokens, fp, repo),
        )
        self.conn.commit()

    def total_cost(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM incidents"
        ).fetchone()
        return row["total"]

    def cost_since(self, since: str) -> float:
        """Total USD spent on incidents last seen at or after `since`.

        Backs the daily spend cap. Keyed on last_seen because that is when the
        cost was actually incurred — an old incident re-analyzed today should
        count against today.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM incidents"
            " WHERE last_seen >= ?",
            (since,),
        ).fetchone()
        return row["total"]

    def total_tokens(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) as prompt,"
            " COALESCE(SUM(completion_tokens), 0) as completion"
            " FROM incidents"
        ).fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    def forget(self, fp: str, repo: str = NO_REPO) -> None:
        """Drop one repo's incident so a future poll treats the error as new."""
        self.conn.execute(
            "DELETE FROM incidents WHERE fingerprint = ? AND repo = ?", (fp, repo)
        )
        self.conn.commit()

    def mark_failed(self, fp: str, repo: str = NO_REPO) -> None:
        """Mark an attempt as failed and count it toward the retry limit."""
        self.conn.execute(
            "UPDATE incidents SET status = 'failed', attempts = attempts + 1"
            " WHERE fingerprint = ? AND repo = ?",
            (fp, repo),
        )
        self.conn.commit()

    def exhausted(self) -> list[dict]:
        """Incidents that failed MAX_ATTEMPTS times and are no longer retried."""
        rows = self.conn.execute(
            "SELECT * FROM incidents WHERE status = 'failed' AND attempts >= ?"
            " ORDER BY last_seen DESC",
            (MAX_ATTEMPTS,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get(self, fp: str, repo: str = NO_REPO) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ? AND repo = ?", (fp, repo)
        ).fetchone()
        return dict(row) if row else None

    def all(self, repo: str | None = None) -> list[dict]:
        """Every incident, newest sighting first; one repo's when `repo` is given."""
        if repo is None:
            rows = self.conn.execute(
                "SELECT * FROM incidents ORDER BY last_seen DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM incidents WHERE repo = ? ORDER BY last_seen DESC",
                (repo,),
            ).fetchall()
        return [dict(r) for r in rows]

    def repos(self) -> list[str]:
        """Distinct repos that have incidents, so a caller can offer a filter."""
        rows = self.conn.execute(
            "SELECT DISTINCT repo FROM incidents ORDER BY repo"
        ).fetchall()
        return [row["repo"] for row in rows]

    def close(self) -> None:
        self.conn.close()
