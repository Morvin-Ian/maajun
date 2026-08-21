from __future__ import annotations

import json

from maajun.agent.tools.base import Tool, json_schema
from maajun.chat.memory import ChatMemory
from maajun.providers.base import ToolDefinition
from maajun.utils import truncate

# Messages are replayed for context, not verbatim transcription.
MESSAGE_PREVIEW = 600

# Ceiling on what one recall call may ask the database for. cap_result already
# bounds what reaches the model, but a limit of 100000 still makes the query
# and builds every row first.
MAX_LIMIT = 100


def clamp(limit: int, default: int) -> int:
    """A model-supplied limit, kept sane. Non-numeric falls back to default."""
    try:
        return max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        return default


def recall_tools(memory: ChatMemory, current_session: int) -> list[Tool]:
    """Build the recall tools bound to the session currently running."""

    async def search_conversations(
        query: str, limit: int = 10, since: str = "", until: str = ""
    ) -> str:
        hits = memory.search(
            query,
            limit=clamp(limit, 10),
            exclude_session=current_session,
            since=since,
            until=until,
        )
        if not hits:
            return f"No earlier conversation mentions {query!r}."
        return json.dumps({"matches": hits}, indent=2)

    async def recall_session(session_id: int = 0, limit: int = 30) -> str:
        if not session_id:
            sessions = memory.recent_sessions(limit=clamp(limit, 30))
            listed = [
                {
                    "session_id": row["id"],
                    "title": row["title"] or "(untitled)",
                    "updated_at": row["updated_at"],
                    "messages": row["message_count"],
                    "cost_usd": round(row["cost_usd"] or 0, 6),
                }
                for row in sessions
                if row["id"] != current_session
            ]
            if not listed:
                return "No earlier chat sessions."
            return json.dumps({"sessions": listed}, indent=2)

        if memory.session(session_id) is None:
            return f"No chat session {session_id}."
        messages = memory.messages(session_id, limit=clamp(limit, 30))
        return json.dumps({
            "session_id": session_id,
            "messages": [
                {
                    "role": message["role"],
                    "content": truncate(message["content"], MESSAGE_PREVIEW, "…"),
                }
                for message in messages
            ],
        }, indent=2)

    return [
        Tool(
            ToolDefinition(
                name="search_conversations",
                description=(
                    "Search earlier chat sessions for something discussed "
                    "before. Use when the user refers to a past conversation "
                    "('what did we decide about…', 'like last time')."
                ),
                parameters=json_schema(
                    {
                        "query": {
                            "type": "string",
                            "description": (
                                "Words to look for; a message matching all of "
                                "them, in any order, is a hit"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max matches to return (default 10)",
                        },
                        "since": {
                            "type": "string",
                            "description": (
                                "Only messages from this UTC date onwards, "
                                "e.g. '2026-08-01'"
                            ),
                        },
                        "until": {
                            "type": "string",
                            "description": "Only messages up to this date",
                        },
                    },
                    required=["query"],
                ),
            ),
            search_conversations,
        ),
        Tool(
            ToolDefinition(
                name="recall_session",
                description=(
                    "Read back an earlier chat session. With no session_id, "
                    "lists recent sessions and their ids."
                ),
                parameters=json_schema({
                    "session_id": {
                        "type": "integer",
                        "description": (
                            "Session to read; omit to list recent sessions"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages or sessions (default 30)",
                    },
                }),
            ),
            recall_session,
        ),
    ]
