from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from maajun.daemon.store import connect, has_fts
from maajun.utils import utcnow_iso

log = logging.getLogger(__name__)

WORD = re.compile(r"\w+", re.UNICODE)

# How much of the first user message becomes the session's title.
TITLE_LENGTH = 60

# Enough to judge relevance, short enough that twenty hits still fit.
SNIPPET_LENGTH = 300


def first_hit(content: str, query: str) -> int:
    """Where to centre the snippet: the earliest query word in the content."""
    lowered = content.lower()
    positions = [
        found
        for word in [query, *WORD.findall(query)]
        if (found := lowered.find(word.lower())) >= 0
    ]
    return min(positions, default=-1)


def snippet(content: str, query: str) -> str:
    """The part of `content` around the first match, so a hit reads in context.

    Falls back to the opening of the message when no query word is found as a
    plain substring — FTS5 matches on stemmed, unordered terms, and str.find
    is neither.
    """
    if len(content) <= SNIPPET_LENGTH:
        return content
    position = first_hit(content, query)
    if position < 0:
        return content[:SNIPPET_LENGTH] + "…"
    start = max(0, position - SNIPPET_LENGTH // 3)
    end = min(len(content), start + SNIPPET_LENGTH)
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")


def match_query(query: str) -> str:
    """A user's words as an FTS5 MATCH expression.

    Every word is quoted and ANDed, so punctuation in a traceback cannot be
    read as FTS operators and 'checkout KeyError' finds a message containing
    both, in any order. Each is a prefix match, so half-remembered words
    ('discount' for 'discounts') still land.
    """
    return " AND ".join(f'"{word}"*' for word in WORD.findall(query))


class ChatMemory:

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.conn = connect(self.path)
        self.fts = has_fts(self.conn)

    def start_session(self, title: str = "") -> int:
        now = utcnow_iso()
        cursor = self.conn.execute(
            "INSERT INTO chat_sessions (started_at, updated_at, title)"
            " VALUES (?, ?, ?)",
            (now, now, title),
        )
        self.conn.commit()
        return cursor.lastrowid

    def add_message(self, session_id: int, role: str, content: str) -> None:
        """Append a message and touch the session's updated_at """
        now = utcnow_iso()
        self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        self.conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        if role == "user":
            self.conn.execute(
                "UPDATE chat_sessions SET title = ? WHERE id = ? AND title = ''",
                (content.strip().replace("\n", " ")[:TITLE_LENGTH], session_id),
            )
        self.conn.commit()

    def record_usage(
        self,
        session_id: int,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Accumulate a turn's spend onto the session """
        self.conn.execute(
            "UPDATE chat_sessions SET"
            " prompt_tokens = prompt_tokens + ?,"
            " completion_tokens = completion_tokens + ?,"
            " cost_usd = cost_usd + ?"
            " WHERE id = ?",
            (prompt_tokens, completion_tokens, cost_usd, session_id),
        )
        self.conn.commit()

    def session(self, session_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def messages(self, session_id: int, limit: int | None = None) -> list[dict]:
        """A session's messages oldest-first; the newest `limit` when given."""
        if limit is None:
            rows = self.conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        rows = self.conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_sessions(self, limit: int = 10) -> list[dict]:
        """Sessions by most recent activity, each with its message count."""
        rows = self.conn.execute(
            "SELECT s.*, COUNT(m.id) AS message_count"
            " FROM chat_sessions s"
            " LEFT JOIN chat_messages m ON m.session_id = s.id"
            " GROUP BY s.id ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        limit: int = 20,
        exclude_session: int | None = None,
        since: str = "",
        until: str = "",
    ) -> list[dict]:
        """Messages matching `query`, newest first, with a context snippet.

        `since`/`until` are ISO dates or timestamps, compared as strings
        against created_at — which is what makes "what did we say last week?"
        answerable.
        """
        query = query.strip()
        if not query:
            return []

        where, params = self.content_predicate(query)
        if exclude_session is not None:
            where.append("m.session_id != ?")
            params.append(exclude_session)
        if since:
            where.append("m.created_at >= ?")
            params.append(since)
        if until:
            where.append("m.created_at <= ?")
            params.append(until)
        params.append(limit)

        sql = (
            "SELECT m.id, m.session_id, m.role, m.content, m.created_at,"
            " s.title AS session_title"
            " FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY m.id DESC LIMIT ?"
        )
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # No results to the user, but a missing table and a bad FTS
            # expression look identical from here — say which somewhere.
            log.debug("chat search failed for %r", query, exc_info=True)
            rows = []

        return [
            {
                "session_id": row["session_id"],
                "session_title": row["session_title"],
                "role": row["role"],
                "created_at": row["created_at"],
                "snippet": snippet(row["content"], query),
            }
            for row in rows
        ]

    def content_predicate(self, query: str) -> tuple[list[str], list]:
        """The content predicate: full-text where indexed, LIKE otherwise."""
        match = match_query(query) if self.fts else ""
        if match:
            return (
                [
                    "m.id IN (SELECT rowid FROM chat_messages_fts"
                    " WHERE chat_messages_fts MATCH ?)"
                ],
                [match],
            )
        return ["m.content LIKE ? ESCAPE '\\'"], [f"%{escape_like(query)}%"]

    def total_cost(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM chat_sessions"
        ).fetchone()
        return row["total"]

    def cost_since(self, since: str) -> float:
        """Chat spend on sessions active at or after `since`, for the cap."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM chat_sessions"
            " WHERE updated_at >= ?",
            (since,),
        ).fetchone()
        return row["total"]

    def delete_session(self, session_id: int) -> bool:
        """Erase one conversation. Returns False if there was no such session."""
        if self.session(session_id) is None:
            return False
        self.conn.execute(
            "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
        )
        self.conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        self.conn.commit()
        return True

    def delete_all(self) -> int:
        """Erase every conversation. Returns how many were deleted."""
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM chat_sessions"
        ).fetchone()["n"]
        self.conn.execute("DELETE FROM chat_messages")
        self.conn.execute("DELETE FROM chat_sessions")
        self.conn.commit()
        return count

    def close(self) -> None:
        self.conn.close()


def escape_like(value: str) -> str:
    """Neutralize LIKE wildcards so a literal % or _ matches itself."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
