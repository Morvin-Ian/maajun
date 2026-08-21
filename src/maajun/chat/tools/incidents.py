
from __future__ import annotations

import json

from maajun.agent.tools.base import Tool, json_schema
from maajun.daemon.store import MAX_ATTEMPTS, IncidentStore
from maajun.providers.base import ToolDefinition
from maajun.utils import truncate

# Reports run long, so a listing carries a preview and get_incident the rest.
REPORT_PREVIEW = 400

LOCAL = "(local)"


def summarize(row: dict, *, preview: bool = True) -> dict:
    """One incident as the model sees it, with the noisy columns dropped."""
    summary = {
        "fingerprint": row["fingerprint"],
        "repo": row["repo"] or LOCAL,
        "status": row["status"],
        "error": row["message"],
        "source": row["source"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "times_seen": row["count"],
        "artifact": row["artifact_kind"] or None,
        "url": row["pr_url"] or None,
        "cost_usd": round(row["cost_usd"] or 0, 6),
    }
    if row["status"] == "failed":
        summary["attempts"] = f"{row['attempts']} of {MAX_ATTEMPTS}"
    report = row["report_text"] or ""
    if report:
        summary["report"] = truncate(report, REPORT_PREVIEW, "…") if preview else report
    return summary


def matches(row: dict, query: str) -> bool:
    """Every word of the query appears somewhere in the incident.

    Words, not the whole string: an incident is referred to from memory
    ("that checkout KeyError"), never in the order the report happens to
    use.
    """
    haystack = " ".join([
        row["fingerprint"], row["repo"], row["message"], row["source"],
        row["status"], row["artifact_kind"] or "", row["report_text"] or "",
        row["pr_url"] or "",
    ]).lower()
    return all(word in haystack for word in query.lower().split())


def incident_tools(store: IncidentStore) -> list[Tool]:
    """Build the incident tools against an open store."""

    async def search_incidents(
        query: str = "",
        repo: str = "",
        status: str = "",
        artifact: str = "",
        since: str = "",
        until: str = "",
        limit: int = 10,
    ) -> str:
        rows = store.all(repo or None)
        if query:
            rows = [row for row in rows if matches(row, query)]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if artifact:
            rows = [row for row in rows if (row["artifact_kind"] or "") == artifact]
        if since:
            rows = [row for row in rows if row["last_seen"] >= since]
        if until:
            rows = [row for row in rows if row["last_seen"] <= until]
        if not rows:
            known = [name for name in store.repos() if name]
            hint = f" Repos with incidents: {', '.join(known)}." if known else ""
            return f"No incidents matched.{hint}"
        total = len(rows)
        rows = rows[: max(1, limit)]
        payload = {
            "matched": total,
            "showing": len(rows),
            "incidents": [summarize(row) for row in rows],
        }
        return json.dumps(payload, indent=2)

    async def get_incident(fingerprint: str, repo: str = "") -> str:
        row = store.get(fingerprint, repo)
        if row is None:
            # Keyed by (fingerprint, repo), where '' is local mode, not "any".
            elsewhere = [
                other for other in store.all()
                if other["fingerprint"] == fingerprint
            ]
            if elsewhere:
                repos = ", ".join(other["repo"] or LOCAL for other in elsewhere)
                return (
                    f"No incident {fingerprint} in repo '{repo or LOCAL}', but it "
                    f"exists for: {repos}. Pass the matching repo."
                )
            return f"No incident with fingerprint {fingerprint}."
        return json.dumps(summarize(row, preview=False), indent=2)

    async def incident_stats() -> str:
        rows = store.all()
        by_status: dict[str, int] = {}
        by_repo: dict[str, int] = {}
        for row in rows:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
            label = row["repo"] or LOCAL
            by_repo[label] = by_repo.get(label, 0) + 1
        tokens = store.total_tokens()
        return json.dumps({
            "total_incidents": len(rows),
            "by_status": by_status,
            "by_repo": by_repo,
            "exhausted": len(store.exhausted()),
            "total_cost_usd": round(store.total_cost(), 6),
            "prompt_tokens": tokens["prompt_tokens"],
            "completion_tokens": tokens["completion_tokens"],
        }, indent=2)

    return [
        Tool(
            ToolDefinition(
                name="search_incidents",
                description=(
                    "Search incidents maajun has handled — the error, its "
                    "analysis, and the pull request or issue it opened. Use "
                    "this for any question about past errors, PRs, issues, or "
                    "what something cost. An empty query lists the most recent."
                ),
                parameters=json_schema({
                    "query": {
                        "type": "string",
                        "description": (
                            "Words to match against the error, report, "
                            "fingerprint, URL, or source; all must appear, in "
                            "any order. Omit to list recent ones."
                        ),
                    },
                    "repo": {
                        "type": "string",
                        "description": "Limit to one repository (owner/name)",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["new", "processed", "failed"],
                        "description": "Limit to one incident status",
                    },
                    "artifact": {
                        "type": "string",
                        "enum": ["pr", "issue", "report"],
                        "description": (
                            "Limit to incidents that produced a pull request, "
                            "a GitHub issue, or a local report file"
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Only incidents last seen on or after this UTC "
                            "date, e.g. '2026-08-01'"
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": "Only incidents last seen on or before this date",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max incidents to return (default 10)",
                    },
                }),
            ),
            search_incidents,
        ),
        Tool(
            ToolDefinition(
                name="get_incident",
                description=(
                    "Fetch one incident in full, including its complete "
                    "analysis report. Use after search_incidents to read the "
                    "root cause rather than the preview."
                ),
                parameters=json_schema(
                    {
                        "fingerprint": {
                            "type": "string",
                            "description": "The incident's fingerprint",
                        },
                        "repo": {
                            "type": "string",
                            "description": (
                                "The repository it was filed against; omit for "
                                "a local-mode incident"
                            ),
                        },
                    },
                    required=["fingerprint"],
                ),
            ),
            get_incident,
        ),
        Tool(
            ToolDefinition(
                name="incident_stats",
                description=(
                    "Totals across every incident: counts by status and repo, "
                    "how many are exhausted, and total spend."
                ),
                parameters=json_schema({}),
            ),
            incident_stats,
        ),
    ]
