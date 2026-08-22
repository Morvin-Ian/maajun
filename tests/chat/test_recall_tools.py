import json

import pytest

from maajun.chat.memory import ChatMemory
from maajun.chat.tools.incidents import incident_tools
from maajun.chat.tools.recall import recall_tools
from maajun.daemon.store import ARTIFACT_ISSUE, ARTIFACT_PR, MAX_ATTEMPTS, IncidentStore
from maajun.monitors import ErrorEvent


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def tool(tools, name):
    return next(t for t in tools if t.definition.name == name).executor


def add(store, fingerprint, *, repo="", message="boom", report="", **kwargs):
    store.record(ErrorEvent(
        source="logfile:/x.log", message=message, details=message,
        fingerprint=fingerprint, repo=repo,
    ))
    if kwargs.get("failures"):
        for _ in range(kwargs["failures"]):
            store.mark_failed(fingerprint, repo)
        return
    store.mark_processed(
        fingerprint, repo,
        branch=kwargs.get("branch", ""),
        pr_url=kwargs.get("url", ""),
        cost_usd=kwargs.get("cost", 0.0),
        report_text=report,
        artifact_kind=kwargs.get("artifact", ""),
    )


# ---------------------------------------------------------------------------
# search_incidents
# ---------------------------------------------------------------------------


async def test_search_matches_the_error_text(store):
    add(store, "fp1", repo="acme/api", message="KeyError: discount")
    add(store, "fp2", repo="acme/api", message="TimeoutError on payments")

    result = await tool(incident_tools(store), "search_incidents")(query="discount")
    payload = json.loads(result)
    assert payload["matched"] == 1
    assert payload["incidents"][0]["fingerprint"] == "fp1"


async def test_search_matches_inside_the_stored_report(store):
    """The point of keeping report_text: recall by root cause, not just title."""
    add(store, "fp1", repo="acme/api", report="root cause: cart/totals.py:88")

    result = await tool(incident_tools(store), "search_incidents")(query="totals.py")
    assert json.loads(result)["matched"] == 1


async def test_search_is_case_insensitive(store):
    add(store, "fp1", repo="acme/api", message="KeyError: discount")

    result = await tool(incident_tools(store), "search_incidents")(query="keyerror")
    assert json.loads(result)["matched"] == 1


async def test_an_empty_query_lists_recent_incidents(store):
    for n in range(3):
        add(store, f"fp{n}", repo="acme/api")

    payload = json.loads(await tool(incident_tools(store), "search_incidents")())
    assert payload["matched"] == 3


async def test_search_filters_by_repo(store):
    add(store, "fp1", repo="acme/api")
    add(store, "fp2", repo="acme/web")

    result = await tool(incident_tools(store), "search_incidents")(repo="acme/web")
    payload = json.loads(result)
    assert [i["repo"] for i in payload["incidents"]] == ["acme/web"]


async def test_search_filters_by_artifact_kind(store):
    """'which PRs have you raised?' is an artifact query, not a text search."""
    add(store, "fp1", repo="acme/api", url="https://gh/pr/1", artifact=ARTIFACT_PR)
    add(store, "fp2", repo="acme/api", url="https://gh/i/2", artifact=ARTIFACT_ISSUE)

    result = await tool(incident_tools(store), "search_incidents")(artifact="pr")
    payload = json.loads(result)
    assert [i["fingerprint"] for i in payload["incidents"]] == ["fp1"]
    assert payload["incidents"][0]["url"] == "https://gh/pr/1"


async def test_search_filters_by_status(store):
    add(store, "fp1", repo="acme/api")
    add(store, "fp2", repo="acme/api", failures=1)

    result = await tool(incident_tools(store), "search_incidents")(status="failed")
    assert json.loads(result)["incidents"][0]["fingerprint"] == "fp2"


async def test_search_reports_the_total_when_limiting(store):
    for n in range(5):
        add(store, f"fp{n}", repo="acme/api")

    payload = json.loads(
        await tool(incident_tools(store), "search_incidents")(limit=2)
    )
    assert payload["matched"] == 5
    assert payload["showing"] == 2


async def test_no_match_names_the_repos_that_do_have_incidents(store):
    add(store, "fp1", repo="acme/api")

    result = await tool(incident_tools(store), "search_incidents")(query="nothing")
    assert "No incidents matched" in result
    assert "acme/api" in result


async def test_a_long_report_is_previewed_not_dumped(store):
    from maajun.chat.tools.incidents import REPORT_PREVIEW

    add(store, "fp1", repo="acme/api", report="x" * 5000)

    payload = json.loads(
        await tool(incident_tools(store), "search_incidents")(query="fp1")
    )
    assert len(payload["incidents"][0]["report"]) <= REPORT_PREVIEW + 1


async def test_a_failed_incident_shows_its_attempt_count(store):
    add(store, "fp1", repo="acme/api", failures=MAX_ATTEMPTS)

    payload = json.loads(
        await tool(incident_tools(store), "search_incidents")(status="failed")
    )
    assert payload["incidents"][0]["attempts"] == f"{MAX_ATTEMPTS} of {MAX_ATTEMPTS}"


async def test_a_local_incident_is_labelled_not_blank(store):
    add(store, "fp1", repo="")

    payload = json.loads(await tool(incident_tools(store), "search_incidents")())
    assert payload["incidents"][0]["repo"] == "(local)"


# ---------------------------------------------------------------------------
# get_incident
# ---------------------------------------------------------------------------


async def test_get_incident_returns_the_full_report(store):
    add(store, "fp1", repo="acme/api", report="y" * 5000)

    result = await tool(incident_tools(store), "get_incident")(
        fingerprint="fp1", repo="acme/api"
    )
    assert len(json.loads(result)["report"]) == 5000


async def test_get_incident_with_the_wrong_repo_says_where_it_lives(store):
    """(fingerprint, repo) is the key, and '' means local rather than 'any'."""
    add(store, "fp1", repo="acme/api")

    result = await tool(incident_tools(store), "get_incident")(fingerprint="fp1")
    assert "acme/api" in result
    assert "Pass the matching repo" in result


async def test_get_incident_unknown_fingerprint(store):
    result = await tool(incident_tools(store), "get_incident")(fingerprint="nope")
    assert "No incident with fingerprint nope" in result


# ---------------------------------------------------------------------------
# incident_stats
# ---------------------------------------------------------------------------


async def test_stats_counts_by_status_and_repo(store):
    add(store, "fp1", repo="acme/api", cost=0.01)
    add(store, "fp2", repo="acme/web", cost=0.02)
    add(store, "fp3", repo="acme/api", failures=MAX_ATTEMPTS)

    payload = json.loads(await tool(incident_tools(store), "incident_stats")())
    assert payload["total_incidents"] == 3
    assert payload["by_status"]["processed"] == 2
    assert payload["by_repo"]["acme/api"] == 2
    assert payload["exhausted"] == 1
    assert abs(payload["total_cost_usd"] - 0.03) < 1e-9


async def test_stats_on_an_empty_store(store):
    payload = json.loads(await tool(incident_tools(store), "incident_stats")())
    assert payload["total_incidents"] == 0
    assert payload["total_cost_usd"] == 0


# ---------------------------------------------------------------------------
# Conversation recall
# ---------------------------------------------------------------------------


@pytest.fixture
def memory(tmp_path):
    m = ChatMemory(tmp_path / "incidents.db")
    yield m
    m.close()


async def test_search_conversations_finds_an_earlier_session(memory):
    old = memory.start_session()
    memory.add_message(old, "assistant", "we set acme/api to fix mode")
    current = memory.start_session()

    result = await tool(recall_tools(memory, current), "search_conversations")(
        query="fix mode"
    )
    assert json.loads(result)["matches"][0]["session_id"] == old


async def test_search_conversations_ignores_the_live_session(memory):
    """The current conversation is already in context; recall is for the rest."""
    current = memory.start_session()
    memory.add_message(current, "user", "fix mode please")

    result = await tool(recall_tools(memory, current), "search_conversations")(
        query="fix mode"
    )
    assert "No earlier conversation" in result


async def test_recall_session_lists_sessions_when_given_no_id(memory):
    old = memory.start_session()
    memory.add_message(old, "user", "checkout triage")
    current = memory.start_session()

    payload = json.loads(
        await tool(recall_tools(memory, current), "recall_session")()
    )
    assert [s["session_id"] for s in payload["sessions"]] == [old]
    assert payload["sessions"][0]["title"] == "checkout triage"


async def test_recall_session_reads_back_the_messages(memory):
    old = memory.start_session()
    memory.add_message(old, "user", "why did checkout 500?")
    memory.add_message(old, "assistant", "empty carts hit a KeyError")
    current = memory.start_session()

    payload = json.loads(
        await tool(recall_tools(memory, current), "recall_session")(session_id=old)
    )
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
    assert "KeyError" in payload["messages"][1]["content"]


async def test_recall_session_truncates_a_very_long_message(memory):
    from maajun.chat.tools.recall import MESSAGE_PREVIEW

    old = memory.start_session()
    memory.add_message(old, "assistant", "z" * 5000)
    current = memory.start_session()

    payload = json.loads(
        await tool(recall_tools(memory, current), "recall_session")(session_id=old)
    )
    assert len(payload["messages"][0]["content"]) <= MESSAGE_PREVIEW + 1


async def test_recall_session_unknown_id(memory):
    current = memory.start_session()
    result = await tool(recall_tools(memory, current), "recall_session")(
        session_id=999
    )
    assert "No chat session 999" in result


async def test_recall_session_with_nothing_to_recall(memory):
    current = memory.start_session()
    result = await tool(recall_tools(memory, current), "recall_session")()
    assert "No earlier chat sessions" in result


async def test_conversations_can_be_searched_by_date(memory):
    old = memory.start_session()
    memory.add_message(old, "assistant", "the checkout 500 was a KeyError")
    search = tool(recall_tools(memory, memory.start_session()), "search_conversations")

    assert "No earlier conversation" in await search(
        query="checkout", until="2000-01-01"
    )
    assert "No earlier conversation" not in await search(
        query="checkout", since="2000-01-01"
    )


async def test_conversations_are_searched_by_word_not_by_substring(memory):
    """'that checkout KeyError' is how anyone refers back to an incident."""
    old = memory.start_session()
    memory.add_message(old, "assistant", "the checkout 500 was a KeyError on carts")
    search = tool(recall_tools(memory, memory.start_session()), "search_conversations")

    assert json.loads(await search(query="KeyError checkout"))["matches"]


async def test_incidents_are_matched_on_every_word_in_any_order(store):
    add(store, "fp1", repo="acme/api", message="KeyError: discount",
         report="the checkout total reads cart['discount'] directly")
    add(store, "fp2", repo="acme/api", message="TimeoutError on payments")

    search = tool(incident_tools(store), "search_incidents")
    payload = json.loads(await search(query="checkout KeyError"))
    assert payload["matched"] == 1
    assert payload["incidents"][0]["fingerprint"] == "fp1"


async def test_incidents_can_be_limited_to_a_date_range(store):
    add(store, "fp1", repo="acme/api", message="KeyError: discount")
    search = tool(incident_tools(store), "search_incidents")

    assert "No incidents matched" in await search(until="2000-01-01")
    assert "No incidents matched" not in await search(since="2000-01-01")
