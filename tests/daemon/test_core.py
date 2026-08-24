"""The watching loop: what reaches an investigation, and what never does.

Deduplication, the spend caps, the per-cycle bound, several repos in one
daemon, shutdown, and the two cheap by-design passes. What happens to an
error once the daemon decides to spend on it is `test_investigation`.
"""

import asyncio
from pathlib import Path

import pytest

from daemon.fakes import (
    REPORT,
    TRACEBACK,
    FakeAgent,
    FakeGitHub,
)
from maajun.config import (
    Config,
    DaemonConfig,
    GitHubConfig,
    MonitorConfig,
    RepoConfig,
)
from maajun.daemon.core import Daemon, make_permission_policy
from maajun.daemon.store import ARTIFACT_ISSUE, ARTIFACT_PR, IncidentStore
from maajun.monitors import LogFileMonitor
from maajun.providers.base import CompletionResponse
from maajun.vcs import GitWorkspace


async def test_same_error_is_reported_once(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert len(github.issues) == 1


async def test_failed_incident_is_marked_and_loop_survives(setup):
    daemon, logfile, agent, github, store, remote = setup

    async def boom(*args, **kwargs):
        raise RuntimeError("github down")

    github.create_issue = boom

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    row = store.all()[0]
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# Permission policies
# ---------------------------------------------------------------------------


def test_suggest_mode_has_no_approvals(tmp_path):
    assert make_permission_policy("suggest", tmp_path) is None


async def test_fix_mode_allows_edits_inside_workspace_only(tmp_path):
    approve = make_permission_policy("fix", tmp_path)

    assert await approve("edit_file", {"path": str(tmp_path / "src" / "a.py")}) is True
    assert await approve("write_file", {"path": str(tmp_path / "new.py")}) is True
    for denied in (
        {"path": "/etc/passwd"},
        {"path": str(tmp_path.parent / "outside.py")},
        {},
    ):
        assert await approve("edit_file", denied) is not True
    assert await approve("bash", {"command": "rm -rf /"}) is not True


async def test_a_denial_tells_the_model_what_to_do_instead(tmp_path):
    """A bare "denied" made the agent retry the same call and give up on the
    fix; the refusal has to name the call that would work."""
    approve = make_permission_policy("fix", tmp_path)

    outside = await approve("edit_file", {"path": "/etc/passwd"})
    assert str(tmp_path) in outside

    other_tool = await approve("bash", {"command": "pytest"})
    assert "edit_file" in other_tool


async def test_notices_emitted_for_new_error_and_artifact(setup):
    daemon, logfile, agent, github, store, remote = setup
    notices: list[tuple[str, str]] = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    levels = [lvl for lvl, _ in notices]
    assert "info" in levels  # new error detected
    assert "success" in levels  # issue opened
    assert any("Issue opened" in msg for _, msg in notices)


async def test_agent_closed_after_incident(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert agent.closed is True  # provider HTTP client freed per incident


async def test_aclose_closes_github_and_monitors(setup):
    daemon, logfile, agent, github, store, remote = setup
    await daemon.aclose()
    assert github.closed is True


async def test_notice_emitted_on_failure(setup):
    daemon, logfile, agent, github, store, remote = setup
    notices: list[tuple[str, str]] = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    async def boom(*args, **kwargs):
        raise RuntimeError("github down")

    github.create_issue = boom

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert any(lvl == "error" for lvl, _ in notices)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def bare_daemon():
    config = Config(
        github=GitHubConfig(repos=[RepoConfig(repo="owner/name", base_branch="main")]),
        monitor=MonitorConfig(log_files=["/dev/null"], poll_interval=9999),
    )
    workspace = GitWorkspace(Path("/tmp/ws"), "owner/name", remote_url="http://x")
    store = IncidentStore(Path("/tmp/does-not-exist/test.db"))
    return Daemon(
        config,
        monitors=[],
        store=store,
        workspaces={"owner/name": workspace},
        monitor_to_repo={},
        github=None,
        agent_factory_for_repo=lambda rc, ws: lambda: None,
    )


async def test_shutdown_event_stops_daemon():
    """Daemon.run exits cleanly once its shutdown event is set."""
    daemon = bare_daemon()

    async def set_shutdown():
        await asyncio.sleep(0.05)
        daemon.shutdown.set()

    await asyncio.gather(daemon.run(), set_shutdown())


async def test_a_second_daemon_is_not_shut_down_by_the_first():
    """The event used to be module-global, so once anything had shut down,
    every later Daemon in the process returned from run() without polling."""
    first = bare_daemon()
    await asyncio.gather(first.run(), stop_soon(first))
    assert first.shutdown.is_set()

    second = bare_daemon()
    assert not second.shutdown.is_set()
    polled = []
    second.poll_once = lambda **kw: polled.append(kw) or asyncio.sleep(0)
    await asyncio.gather(second.run(), stop_soon(second))
    assert polled, "the second daemon should still do a poll cycle"


async def stop_soon(daemon):
    await asyncio.sleep(0.05)
    daemon.shutdown.set()


async def test_signal_handlers_are_removed_when_run_returns(monkeypatch):
    """The loop outlives one daemon; a handler left pointing at a finished
    daemon's event would silently do nothing for the next one."""
    daemon = bare_daemon()
    added, removed = [], []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, cb: added.append(sig)
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))

    await daemon.run(once=True)
    assert added and added == removed


async def test_a_loop_without_signal_handlers_still_runs(monkeypatch):
    """Windows' proactor loop raises NotImplementedError; Ctrl-C arrives as
    KeyboardInterrupt there instead."""
    daemon = bare_daemon()
    loop = asyncio.get_running_loop()

    def unsupported(sig, cb):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    monkeypatch.setattr(
        loop, "remove_signal_handler", lambda sig: pytest.fail("nothing to remove")
    )

    await daemon.run(once=True)


# ---------------------------------------------------------------------------
# Daily spend cap
# ---------------------------------------------------------------------------


def seed_spend(store, fingerprint: str, cost: float) -> None:
    """Record an already-paid-for incident so today's spend is non-zero."""
    from maajun.monitors import ErrorEvent

    store.record(ErrorEvent(
        source="test", message="prior", details="prior", fingerprint=fingerprint,
    ))
    store.mark_processed(fingerprint, branch="", pr_url="x", cost_usd=cost)


async def test_spend_cap_stops_further_analysis(setup):
    """An unattended daemon must not be an unbounded bill."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    seed_spend(store, "earlier", 0.02)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert github.issues == []
    assert agent.prompts == []  # no AI call was made


async def test_capped_error_is_retried_later(setup):
    """Skipping for budget must not blacklist the error forever."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    seed_spend(store, "earlier", 0.02)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    # Cap raised (or a new day) — the same error is picked up again.
    daemon.config.daemon.max_usd_per_day = 100.0
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(github.issues) == 1


async def test_spend_cap_warns_once_per_day(setup):
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    seed_spend(store, "earlier", 0.05)
    notices: list[tuple[str, str]] = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    for _ in range(3):
        with open(logfile, "a") as f:
            f.write(TRACEBACK)
        await daemon.poll_once()

    warnings = [msg for lvl, msg in notices if lvl == "warn"]
    assert len(warnings) == 1
    assert "spend cap reached" in warnings[0]
    assert "$0.01" in warnings[0]


async def test_capped_by_default(setup):
    """An install nobody tuned is still bounded — the default is a ceiling."""
    daemon, logfile, agent, github, store, remote = setup
    assert daemon.config.daemon.max_usd_per_day == 5.0
    seed_spend(store, "earlier", 999.0)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    assert await daemon.poll_once() == []
    assert agent.prompts == []


async def test_zero_disables_the_cap(setup):
    """0 is the opt-out, for someone who wants an unbounded daemon."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.0
    seed_spend(store, "earlier", 999.0)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    assert len(await daemon.poll_once()) == 1


async def test_dry_run_ignores_the_cap(setup):
    """--dry-run costs money too, but it's an explicit interactive request."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    seed_spend(store, "earlier", 0.02)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once(dry_run=True)

    assert agent.prompts  # the analysis still ran


async def test_cap_warning_reports_the_configured_amount(setup):
    """A sub-cent cap must not be rounded up in the warning."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.005
    seed_spend(store, "earlier", 0.02)
    notices: list[tuple[str, str]] = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    warning = next(msg for lvl, msg in notices if lvl == "warn")
    assert "$0.005" in warning
    assert "$0.01" not in warning


# ---------------------------------------------------------------------------
# Per-cycle incident bound
# ---------------------------------------------------------------------------


DISTINCT_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def distinct_errors(count: int) -> str:
    """Errors with genuinely different fingerprints.

    Numbered messages ("failure 1", "failure 2") all collapse to one incident,
    because fingerprinting strips digits so the same crash at a different line
    number stays one error.
    """
    lines = [f"ERROR {word} subsystem broke" for word in DISTINCT_WORDS[:count]]
    return "\n".join([*lines, "INFO end", ""])


async def test_cycle_limit_bounds_a_burst_of_novel_errors(setup):
    """The daily cap bounds the day; this bounds one poll."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_incidents_per_cycle = 2

    with open(logfile, "a") as f:
        f.write(distinct_errors(5))

    handled = await daemon.poll_once()
    assert len(handled) == 2
    assert len(github.issues) == 2


async def test_errors_beyond_the_cycle_limit_are_picked_up_next_poll(setup):
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_incidents_per_cycle = 2

    with open(logfile, "a") as f:
        f.write(distinct_errors(5))
    await daemon.poll_once()

    # Same lines re-read on the next poll (the deferred ones were forgotten).
    with open(logfile, "a") as f:
        f.write(distinct_errors(5))
    handled = await daemon.poll_once()

    assert len(handled) == 2
    assert len(github.issues) == 4


async def test_cycle_limit_resets_each_poll(setup):
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_incidents_per_cycle = 1

    for word in ("alpha", "beta", "gamma"):
        with open(logfile, "a") as f:
            f.write(f"ERROR {word} broke\nINFO end\n")
        assert len(await daemon.poll_once()) == 1


async def test_zero_means_unlimited(setup):
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_incidents_per_cycle = 0

    with open(logfile, "a") as f:
        f.write(distinct_errors(4))

    assert len(await daemon.poll_once()) == 4


async def test_cycle_limit_counts_failed_attempts_too(setup):
    """A failed incident still made an AI call, so it must count.

    Regression: the counter incremented only on success, so a run where every
    incident failed analyzed an unbounded number of errors.
    """
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_incidents_per_cycle = 2

    async def boom(*args, **kwargs):
        raise RuntimeError("provider rejected the key")

    github.create_issue = boom

    with open(logfile, "a") as f:
        f.write(distinct_errors(5))
    await daemon.poll_once()

    attempted = [row for row in store.all() if row["status"] == "failed"]
    assert len(attempted) == 2


# ---------------------------------------------------------------------------
# Multi-repo: two repos, one daemon
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_setup(tmp_path, remote):
    """Two repos watching two log files, plus one log file they share.

    Both clone the same bare remote — the test is about routing and
    attribution, not about git.
    """
    shared_log = tmp_path / "shared.log"
    api_log = tmp_path / "api.log"
    for path in (shared_log, api_log):
        path.write_text("")

    api = RepoConfig(
        repo="acme/api", base_branch="main", mode="suggest",
        log_files=[str(shared_log), str(api_log)],
    )
    web = RepoConfig(
        repo="acme/web", base_branch="main", mode="suggest",
        log_files=[str(shared_log)],
    )
    config = Config(
        github=GitHubConfig(repos=[api, web]),
        monitor=MonitorConfig(poll_interval=1),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    store = IncidentStore(tmp_path / "work" / "incidents.db")
    github = FakeGitHub()
    monitors = [
        LogFileMonitor(shared_log),   # -> acme/api
        LogFileMonitor(api_log),      # -> acme/api
        LogFileMonitor(shared_log),   # -> acme/web
    ]
    daemon = Daemon(
        config,
        monitors=monitors,
        store=store,
        workspaces={
            "acme/api": GitWorkspace(
                tmp_path / "work" / "ws", "acme/api", remote_url=str(remote)
            ),
            "acme/web": GitWorkspace(
                tmp_path / "work" / "ws", "acme/web", remote_url=str(remote)
            ),
        },
        monitor_to_repo={
            id(monitors[0]): api, id(monitors[1]): api, id(monitors[2]): web,
        },
        github=github,
        agent_factory_for_repo=lambda rc, ws: lambda: FakeAgent(
            edit_path=(ws.path / "main.py") if rc.mode == "fix" else None
        ),
        repo_configs=[api, web],
    )
    return daemon, shared_log, api_log, github, store


async def test_a_shared_log_file_files_an_issue_in_every_repo(multi_setup):
    """Regression: the second repo's copy was dropped as an already-known error."""
    daemon, shared_log, _, github, store = multi_setup

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    await daemon.poll_once()

    assert sorted(issue["repo"] for issue in github.issues) == ["acme/api", "acme/web"]


async def test_each_repos_incident_is_recorded_against_that_repo(multi_setup):
    daemon, shared_log, _, github, store = multi_setup

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    handled = await daemon.poll_once()
    fp = handled[0]

    assert store.get(fp, "acme/api")["pr_url"] == "https://github.com/acme/api/issues/1"
    assert store.get(fp, "acme/web")["pr_url"] == "https://github.com/acme/web/issues/2"


async def test_an_error_in_one_repos_own_log_stays_in_that_repo(multi_setup):
    daemon, _, api_log, github, store = multi_setup

    with open(api_log, "a") as f:
        f.write(TRACEBACK)

    await daemon.poll_once()

    assert [issue["repo"] for issue in github.issues] == ["acme/api"]


async def test_the_issue_body_names_the_repo_the_error_was_attributed_to(multi_setup):
    """`source` says which log file saw it, which is not the same thing."""
    daemon, shared_log, _, github, store = multi_setup

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    await daemon.poll_once()

    by_repo = {issue["repo"]: issue["body"] for issue in github.issues}
    assert "- Repo: `acme/api`" in by_repo["acme/api"]
    assert "- Repo: `acme/web`" in by_repo["acme/web"]
    assert f"- Source: `logfile:{shared_log}`" in by_repo["acme/web"]


async def test_notices_name_the_repo(multi_setup):
    daemon, shared_log, _, github, store = multi_setup
    notices = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    await daemon.poll_once()

    assert "New error in acme/api:" in "\n".join(m for _, m in notices)
    assert "New error in acme/web:" in "\n".join(m for _, m in notices)
    assert "Issue opened for acme/web:" in "\n".join(m for _, m in notices)


async def test_per_repo_mode_is_honoured_independently(multi_setup):
    """acme/web in fix mode opens a PR; acme/api stays on issues."""
    daemon, shared_log, _, github, store = multi_setup
    daemon.repo_for(daemon.monitors[2]).mode = "fix"

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    await daemon.poll_once()

    assert [issue["repo"] for issue in github.issues] == ["acme/api"]
    assert [call["repo"] for call in github.calls] == ["acme/web"]


async def test_one_repos_failure_does_not_block_the_other(multi_setup):
    """A GitHub error for acme/api must not cost acme/web its issue."""
    daemon, shared_log, _, github, store = multi_setup

    async def create_issue(repo, *, title, body):
        if repo == "acme/api":
            raise RuntimeError("boom")
        github.issues.append({"repo": repo, "title": title, "body": body})
        return f"https://github.com/{repo}/issues/{len(github.issues)}"

    github.create_issue = create_issue

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    handled = await daemon.poll_once()
    fp = handled[0]

    assert [issue["repo"] for issue in github.issues] == ["acme/web"]
    assert store.get(fp, "acme/api")["status"] == "failed"
    assert store.get(fp, "acme/web")["status"] == "processed"


async def test_one_unwired_monitor_does_not_stop_the_others(multi_setup):
    """A monitor with no repo is skipped, not allowed to abort the poll cycle."""
    daemon, shared_log, api_log, github, store = multi_setup
    orphan = LogFileMonitor(api_log)  # deliberately absent from monitor_to_repo
    daemon.monitors = [orphan, *daemon.monitors]
    notices = []
    daemon.on_notice = lambda message, level: notices.append(message)

    with open(shared_log, "a") as f:
        f.write(TRACEBACK)

    await daemon.poll_once()

    assert sorted(issue["repo"] for issue in github.issues) == ["acme/api", "acme/web"]
    assert any("is not usable" in message for message in notices)


def test_artifact_label_covers_every_kind():
    from maajun.daemon.store import ARTIFACT_REPORT

    assert Daemon.artifact_label(ARTIFACT_PR) == "PR opened"
    assert Daemon.artifact_label(ARTIFACT_ISSUE) == "Issue opened"
    assert Daemon.artifact_label(ARTIFACT_REPORT) == "Report written"
    assert Daemon.artifact_label(None) == "Handled"
    assert Daemon.artifact_label("") == "Handled"


async def test_a_capped_error_keeps_its_history(setup):
    """Deferral used to delete the row, resetting the count every poll.

    While the cap held, each poll re-inserted and re-deleted the incident, so
    'seen 40 times over two hours' was reported as 'seen once'.
    """
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    seed_spend(store, "earlier", 0.02)

    for _ in range(3):
        with open(logfile, "a") as f:
            f.write(TRACEBACK)
        await daemon.poll_once()

    deferred = [row for row in store.all() if row["fingerprint"] != "earlier"]
    assert len(deferred) == 1
    assert deferred[0]["count"] == 3
    assert deferred[0]["status"] == "new"
    assert agent.prompts == []  # still no AI call


async def test_first_seen_is_when_the_error_started_not_when_the_cap_lifted(setup):
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    seed_spend(store, "earlier", 0.02)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()
    started = [r for r in store.all() if r["fingerprint"] != "earlier"][0]["first_seen"]

    daemon.config.daemon.max_usd_per_day = 100.0
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    assert store.get(fp, "owner/name")["first_seen"] == started


# ---------------------------------------------------------------------------
# Errors that are the code working
# ---------------------------------------------------------------------------

# One line, the way a monitor sees it: the level marker and the guard's own
# name have to share a line to arrive as one event. The INFO line after it is
# what releases the error from the traceback lookahead.
VALIDATION_ERROR = (
    "ERROR 2026-08-23 10:01:02 django.request: ValidationError on /api/signup: "
    "{'email': ['Enter a valid email address.']}\n"
    "INFO 2026-08-23 10:01:02 django.server: 400 in 4ms\n"
)

BY_DESIGN_REPORT = """# the signup serializer rejects a malformed email

## Verdict
by design — the serializer is meant to refuse this and the view returns 400.

## What happened
A user typed an invalid email and got a 400 back.

## Root cause
None. `api/serializers.py:31` validates the field on purpose.

## Suggested fix
None — working as intended.
"""


async def test_a_guard_firing_as_designed_is_never_analyzed(setup):
    """The signature pass runs before the model, so it costs nothing."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(VALIDATION_ERROR)

    handled = await daemon.poll_once()

    assert handled == []
    assert agent.prompts == []  # never asked
    assert github.issues == []
    row = store.ignored()[0]
    assert row["status"] == "ignored"
    assert "validation" in row["ignored_reason"]
    assert row["cost_usd"] in (0, None)


async def test_an_ignored_error_is_not_re_analyzed_when_it_recurs(setup):
    """The row stays so every later poll is free too."""
    daemon, logfile, agent, github, store, remote = setup

    for _ in range(3):
        with open(logfile, "a") as f:
            f.write(VALIDATION_ERROR)
        await daemon.poll_once()

    assert agent.prompts == []
    assert len(store.ignored()) == 1
    assert store.ignored()[0]["count"] == 3


async def test_the_signature_pass_can_be_turned_off(setup):
    """For a codebase where a validation error really is a bug."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.monitor.ignore_by_design = False

    with open(logfile, "a") as f:
        f.write(VALIDATION_ERROR)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert agent.prompts != []


async def test_a_codebase_can_add_its_own_signature(setup):
    """A paywall is not something the shipped patterns can know about."""
    from maajun.daemon import triage

    daemon, logfile, agent, github, store, remote = setup
    daemon.ignore_patterns = triage.compile_extra([r"PaywallError"])

    with open(logfile, "a") as f:
        f.write("ERROR PaywallError: exports need a paid plan\n")
    handled = await daemon.poll_once()

    assert handled == []
    assert agent.prompts == []


async def test_a_real_defect_still_gets_filed(setup):
    """The whole point: the filter must not swallow a genuine error."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(github.issues) == 1
    assert store.ignored() == []


async def test_the_agent_can_call_an_error_intended_after_reading_the_code(setup):
    """The signatures cannot recognise an app's own guard; the agent can."""
    daemon, logfile, agent, github, store, remote = setup
    agent.report = BY_DESIGN_REPORT

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert agent.prompts != []  # it was analyzed...
    assert github.issues == []  # ...and then not filed
    assert len(handled) == 1
    row = store.ignored()[0]
    assert row["status"] == "ignored"
    assert "serializer" in row["ignored_reason"]


async def test_what_a_by_design_analysis_cost_is_still_banked(setup):
    """The round was billed whether or not anything was published."""
    daemon, logfile, agent, github, store, remote = setup
    agent.report = BY_DESIGN_REPORT
    agent.usage_per_call = {"prompt_tokens": 1_000_000, "completion_tokens": 0}

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    row = store.ignored()[0]
    assert row["prompt_tokens"] == 1_000_000
    assert row["cost_usd"] >= 0


async def test_a_report_with_no_verdict_is_still_filed(setup):
    """Silence must not suppress a report."""
    daemon, logfile, agent, github, store, remote = setup
    agent.report = REPORT  # has no Verdict section

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(github.issues) == 1
    assert store.ignored() == []


# ---------------------------------------------------------------------------
# The cheap screen, between the signatures and the investigation
# ---------------------------------------------------------------------------


class FakeScreen:
    """A one-round, tool-less agent standing in for the cheap tier."""

    model = "claude-haiku-4-5"

    def __init__(self, answer="investigate", fail=False):
        self.answer = answer
        self.fail = fail
        self.prompts = []
        self.closed = False

    async def chat(self, message):
        self.prompts.append(message)
        if self.fail:
            raise RuntimeError("provider down")
        return CompletionResponse(
            content=self.answer,
            usage={"prompt_tokens": 400, "completion_tokens": 8},
        )

    def take_usage(self):
        return {"prompt_tokens": 400, "completion_tokens": 8}

    async def aclose(self):
        self.closed = True


def with_screen(daemon, screen):
    daemon.screen_factory = lambda: screen
    return screen


async def test_the_screen_stops_an_application_guard_before_the_investigation(setup):
    """A paywall is not named after its own intent, so no signature catches
    it — and the verdict from a finished report costs the investigation."""
    daemon, logfile, agent, github, store, remote = setup
    screen = with_screen(daemon, FakeScreen("by design: a plan check refused it"))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert len(screen.prompts) == 1
    assert agent.prompts == []  # the expensive one was never asked
    assert github.issues == []
    assert github.calls == []
    row = store.ignored()[0]
    assert row["status"] == "ignored"
    assert "plan check refused it" in row["ignored_reason"]
    # Cheap is not free, and the day's cap has to see it.
    assert row["cost_usd"] > 0


async def test_a_screen_that_says_investigate_costs_one_small_request(setup):
    daemon, logfile, agent, github, store, remote = setup
    screen = with_screen(daemon, FakeScreen("investigate"))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(screen.prompts) == 1
    assert len(agent.prompts) == 1
    assert len(github.issues) == 1
    assert screen.closed


async def test_a_broken_screen_never_costs_an_error_its_report(setup):
    """If it cannot answer, the error is investigated as it always was."""
    daemon, logfile, agent, github, store, remote = setup
    with_screen(daemon, FakeScreen(fail=True))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(github.issues) == 1


async def test_an_unparseable_screen_answer_reads_as_investigate(setup):
    daemon, logfile, agent, github, store, remote = setup
    with_screen(daemon, FakeScreen("I think this might be by design, but..."))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)

    assert len(await daemon.poll_once()) == 1


async def test_screening_can_be_turned_off(setup):
    daemon, logfile, agent, github, store, remote = setup
    screen = with_screen(daemon, FakeScreen("by design: whatever"))
    daemon.config.daemon.screen_errors = False

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert screen.prompts == []
    assert len(handled) == 1


async def test_the_signatures_still_run_first_and_cost_nothing(setup):
    """The screen is a model call, so a signature match must not reach it."""
    daemon, logfile, agent, github, store, remote = setup
    screen = with_screen(daemon, FakeScreen("investigate"))

    with open(logfile, "a") as f:
        f.write(VALIDATION_ERROR)
    handled = await daemon.poll_once()

    assert handled == []
    assert screen.prompts == []
