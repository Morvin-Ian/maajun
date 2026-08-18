from __future__ import annotations

from pathlib import Path

from maajun.daemon.store import connect
from maajun.utils import utcnow_iso

# How much of the first user message becomes the session's title.
TITLE_LENGTH = 60

# Characters of surrounding message shown per search hit. Long enough to judge
# relevance, short enough that twenty hits still fit in a tool result.
SNIPPET_LENGTH = 300


def _snippet(content: str, query: str) -> str:
    """The part of `content` around the first match, so a hit reads in context.

    Falls back to the opening of the message when the match is not found as a
    plain substring — the SQL LIKE that produced the row is case-insensitive
    for ASCII, and str.find is not.
    """
    if len(content) <= SNIPPET_LENGTH:
        return content
    position = content.lower().find(query.lower())
    if position < 0:
        return content[:SNIPPET_LENGTH] + "…"
    start = max(0, position - SNIPPET_LENGTH // 3)
    end = min(len(content), start + SNIPPET_LENGTH)
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")


class ChatMemory:

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._conn = connect(self.path)

    def start_session(self, title: str = "") -> int:
        now = utcnow_iso()
        cursor = self._conn.execute(
            "INSERT INTO chat_sessions (started_at, updated_at, title)"
            " VALUES (?, ?, ?)",
            (now, now, title),
        )
        self._conn.commit()
        return cursor.lastrowid

    def add_message(self, session_id: int, role: str, content: str) -> None:
        """Append a message and touch the session's updated_at """
        now = utcnow_iso()
        self._conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        self._conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        if role == "user":
            self._conn.execute(
                "UPDATE chat_sessions SET title = ? WHERE id = ? AND title = ''",
                (content.strip().replace("\n", " ")[:TITLE_LENGTH], session_id),
            )
        self._conn.commit()

    def record_usage(
        self,
        session_id: int,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Accumulate a turn's spend onto the session """
        self._conn.execute(
            "UPDATE chat_sessions SET"
            " prompt_tokens = prompt_tokens + ?,"
            " completion_tokens = completion_tokens + ?,"
            " cost_usd = cost_usd + ?"
            " WHERE id = ?",
            (prompt_tokens, completion_tokens, cost_usd, session_id),
        )
        self._conn.commit()

    def session(self, session_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def messages(self, session_id: int, limit: int | None = None) -> list[dict]:
        """A session's messages oldest-first; the newest `limit` when given."""
        if limit is None:
            rows = self._conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        rows = self._conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_sessions(self, limit: int = 10) -> list[dict]:
        """Sessions by most recent activity, each with its message count."""
        rows = self._conn.execute(
            "SELECT s.*, COUNT(m.id) AS message_count"
            " FROM chat_sessions s"
            " LEFT JOIN chat_messages m ON m.session_id = s.id"
            " GROUP BY s.id ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self, query: str, limit: int = 20, exclude_session: int | None = None
    ) -> list[dict]:
        """Messages containing `query`, newest first, with a context snippet """
        query = query.strip()
        if not query:
            return []
        pattern = f"%{_escape_like(query)}%"
        sql = (
            "SELECT m.id, m.session_id, m.role, m.content, m.created_at,"
            " s.title AS session_title"
            " FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id"
            " WHERE m.content LIKE ? ESCAPE '\\'"
        )
        params: list = [pattern]
        if exclude_session is not None:
            sql += " AND m.session_id != ?"
            params.append(exclude_session)
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)

        return [
            {
                "session_id": row["session_id"],
                "session_title": row["session_title"],
                "role": row["role"],
                "created_at": row["created_at"],
                "snippet": _snippet(row["content"], query),
            }
            for row in self._conn.execute(sql, params).fetchall()
        ]

    def total_cost(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM chat_sessions"
        ).fetchone()
        return row["total"]

    def close(self) -> None:
        self._conn.close()


def _escape_like(value: str) -> str:
    """Neutralize LIKE wildcards so a literal % or _ matches itself."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
