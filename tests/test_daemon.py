"""End-to-end daemon tests against a local bare git repo.

Real: monitors, incident store, git workspace (clone/branch/commit/push).
Fake: the AI agent and the GitHub API.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from maajun.config import Config, DaemonConfig, GitHubConfig, MonitorConfig, RepoConfig
from maajun.daemon import SHUTDOWN_EVENT, Daemon, make_permission_policy
from maajun.monitors import LogFileMonitor
from maajun.providers.base import CompletionResponse
from maajun.state import IncidentStore
from maajun.vcs import GitWorkspace

TRACEBACK = """\
Traceback (most recent call last):
  File "/app/main.py", line 42, in handler
    result = items[index]
IndexError: list index out of range
"""

REPORT = "# IndexError in handler\n\n## Root cause\nOff-by-one in main.py."


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


@pytest.fixture
def remote(tmp_path):
    """A bare repo standing in for GitHub, seeded with one commit on main."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)], check=True, capture_output=True
    )
    (seed / "main.py").write_text("items = []\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return bare


class FakeAgent:
    def __init__(self, report=REPORT, edit_path: Path | None = None):
        self.report = report
        self.edit_path = edit_path
        self.prompts: list[str] = []
        self.closed = False

    async def chat(self, message):
        self.prompts.append(message)
        if self.edit_path:
            self.edit_path.write_text("items = [0]\n")
        return CompletionResponse(content=self.report)

    async def aclose(self):
        self.closed = True


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.closed = False

    async def create_pull_request(self, repo, *, head, base, title, body):
        self.calls.append(
            {"repo": repo, "head": head, "base": base, "title": title, "body": body}
        )
        return f"https://github.com/{repo}/pull/{len(self.calls)}"

    async def create_issue(self, repo, *, title, body):
        self.issues.append({"repo": repo, "title": title, "body": body})
        return f"https://github.com/{repo}/issues/{len(self.issues)}"

    async def aclose(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path, remote):
    logfile = tmp_path / "app.log"
    logfile.write_text("")

    repo_config = RepoConfig(repo="owner/name", base_branch="main", mode="suggest")
    config = Config(
        github=GitHubConfig(repos=[repo_config]),
        monitor=MonitorConfig(log_files=[str(logfile)], poll_interval=1),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    workspace = GitWorkspace(tmp_path / "work" / "ws", "owner/name", remote_url=str(remote))
    store = IncidentStore(tmp_path / "work" / "incidents.db")
    agent = FakeAgent()
    github = FakeGitHub()
    monitor = LogFileMonitor(logfile)
    daemon = Daemon(
        config,
        monitors=[monitor],
        store=store,
        workspaces={"owner/name": workspace},
        monitor_to_repo={monitor.name: repo_config},
        github=github,
        agent_factory_for_repo=lambda rc, ws: lambda: agent,
    )
    return daemon, logfile, agent, github, store, remote


async def test_suggest_mode_files_an_issue(setup):
    """An analysis that changes no code is an issue, not an empty-diff PR."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)

    handled = await daemon.poll_once()

    assert len(handled) == 1
    fp = handled[0]

    # Agent got the error and the workspace path
    assert "IndexError" in agent.prompts[0]
    workspace = daemon.workspaces["owner/name"]
    assert str(workspace.path) in agent.prompts[0]

    # An issue carries the report; no PR, no branch, no push.
    assert github.calls == []
    assert len(github.issues) == 1
    issue = github.issues[0]
    assert "IndexError" in issue["title"]
    assert "Root cause" in issue["body"]
    assert "IndexError: list index out of range" in issue["body"]  # error details
    assert store.get(fp)["pr_url"].endswith("/issues/1")
    assert store.get(fp)["branch"] == ""


async def test_suggest_mode_pushes_no_branch(setup):
    """The whole point: no branch to review, no CI run, no empty diff."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    branches = subprocess.run(
        ["git", "branch", "-a"], cwd=str(remote),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "maajun/incident-" not in branches


async def test_fix_mode_opens_a_pull_request_with_the_report_committed(setup):
    """Fix mode has a real diff, so it still gets a branch and a PR."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.monitor_to_repo[daemon.monitors[0].name].mode = "fix"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    assert github.issues == []
    assert len(github.calls) == 1
    call = github.calls[0]
    assert call["head"] == f"maajun/incident-{fp}"
    assert call["base"] == "main"
    assert "IndexError" in call["title"]
    assert "Root cause" in call["body"]

    # Branch with the committed report exists on the remote
    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:docs/incidents/{fp}.md"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.returncode == 0
    assert "Root cause" in show.stdout

    row = store.get(fp)
    assert row["status"] == "processed"
    assert row["pr_url"] == "https://github.com/owner/name/pull/1"
    assert row["branch"] == f"maajun/incident-{fp}"


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


async def test_fix_mode_commits_agent_changes(setup):
    daemon, logfile, agent, github, store, remote = setup
    # Update the repo config mode to "fix"
    repo_config = daemon.monitor_to_repo[list(daemon.monitor_to_repo.keys())[0]]
    repo_config.mode = "fix"
    workspace = daemon.workspaces["owner/name"]
    agent.edit_path = workspace.path / "main.py"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()
    fp = handled[0]

    assert "MAY apply the fix" in agent.prompts[0]

    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:main.py"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.stdout == "items = [0]\n"
    assert "applied fix" in github.calls[0]["body"]


# ---------------------------------------------------------------------------
# Permission policies
# ---------------------------------------------------------------------------


def test_suggest_mode_has_no_approvals(tmp_path):
    assert make_permission_policy("suggest", tmp_path) is None


async def test_fix_mode_allows_edits_inside_workspace_only(tmp_path):
    approve = make_permission_policy("fix", tmp_path)

    assert await approve("edit_file", {"path": str(tmp_path / "src" / "a.py")})
    assert await approve("write_file", {"path": str(tmp_path / "new.py")})
    assert not await approve("edit_file", {"path": "/etc/passwd"})
    assert not await approve("edit_file", {"path": str(tmp_path.parent / "outside.py")})
    assert not await approve("edit_file", {})
    assert not await approve("bash", {"command": "rm -rf /"})


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


async def test_dry_run_skips_git_and_pr(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)

    handled = await daemon.poll_once(dry_run=True)

    assert len(handled) == 1
    fp = handled[0]

    # Agent still analyzed the error
    assert "IndexError" in agent.prompts[0]

    # No branch was created, no PR was opened
    assert github.calls == []
    show = subprocess.run(
        ["git", "branch", "--list", f"maajun/incident-{fp}"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.stdout.strip() == ""

    # Incident not persisted — a real run should still process it
    assert store.get(fp) is None


# ---------------------------------------------------------------------------
# Manual reports, progress, and notices
# ---------------------------------------------------------------------------


async def test_manual_report_opens_pr_and_reports_progress(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.monitor_to_repo[next(iter(daemon.monitor_to_repo))]
    phases: list[str] = []

    url = await daemon.handle_manual_report(
        "Checkout button does nothing", repo_config, progress=phases.append
    )

    assert phases == ["Preparing workspace", "Analyzing with AI", "Filing issue"]
    assert url.endswith("/issues/1")
    assert "Checkout button" in agent.prompts[0]
    assert "Checkout button" in github.issues[0]["title"]


async def test_manual_report_dry_run_only_analyzes(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.monitor_to_repo[next(iter(daemon.monitor_to_repo))]
    phases: list[str] = []

    pr_url = await daemon.handle_manual_report(
        "Something is broken", repo_config, dry_run=True, progress=phases.append
    )

    assert pr_url == ""
    assert phases == ["Analyzing with AI"]  # no workspace prep / PR in dry run
    assert github.calls == []


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


async def test_shutdown_event_stops_daemon():
    """Daemon.run exits cleanly when SHUTDOWN_EVENT is set."""
    SHUTDOWN_EVENT.clear()

    config = Config(
        github=GitHubConfig(repo="owner/name", base_branch="main"),
        monitor=MonitorConfig(log_files=["/dev/null"], poll_interval=9999),
    )
    workspace = GitWorkspace(Path("/tmp/ws"), "owner/name", remote_url="http://x")
    store = IncidentStore(Path("/tmp/does-not-exist/test.db"))
    daemon = Daemon(
        config,
        monitors=[],
        store=store,
        workspaces={"owner/name": workspace},
        monitor_to_repo={},
        github=None,
        agent_factory_for_repo=lambda rc, ws: lambda: None,
    )

    async def set_shutdown():
        await asyncio.sleep(0.05)
        SHUTDOWN_EVENT.set()

    await asyncio.gather(daemon.run(), set_shutdown())
    SHUTDOWN_EVENT.clear()


# ---------------------------------------------------------------------------
# Local mode: no GitHub configured
# ---------------------------------------------------------------------------


@pytest.fixture
def local_setup(tmp_path):
    """A daemon with no GitHub repo — reports go to disk instead of PRs."""
    from maajun.daemon import LocalWorkspace

    logfile = tmp_path / "app.log"
    logfile.write_text("")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "main.py").write_text("items = []\n")

    repo_config = RepoConfig(mode="suggest")
    config = Config(
        github=GitHubConfig(),
        monitor=MonitorConfig(log_files=[str(logfile)], poll_interval=1),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    store = IncidentStore(tmp_path / "work" / "incidents.db")
    agent = FakeAgent()
    monitor = LogFileMonitor(logfile)
    daemon = Daemon(
        config,
        monitors=[monitor],
        store=store,
        workspaces={"": LocalWorkspace(checkout)},
        monitor_to_repo={monitor.name: repo_config},
        github=None,
        agent_factory_for_repo=lambda rc, ws: lambda: agent,
        repo_configs=[repo_config],
        report_dir=tmp_path / "work" / "reports",
        local_mode=True,
    )
    return daemon, logfile, agent, store, checkout


async def test_local_mode_writes_a_report_instead_of_a_pr(local_setup):
    daemon, logfile, agent, store, checkout = local_setup

    with open(logfile, "a") as f:
        f.write("2026-07-18 ERROR shop: order failed\n")
        f.write(TRACEBACK)
        f.write("INFO next\n")

    handled = await daemon.poll_once()
    assert len(handled) == 1

    report_path = daemon.report_dir / f"{handled[0]}.md"
    assert report_path.exists()
    assert REPORT in report_path.read_text()
    assert "IndexError" in report_path.read_text()


async def test_local_mode_analyzes_the_local_checkout(local_setup):
    daemon, logfile, agent, store, checkout = local_setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()
    await daemon.poll_once()  # flush the carried-over traceback

    assert str(checkout) in agent.prompts[0]


async def test_local_mode_records_the_incident_as_processed(local_setup):
    daemon, logfile, agent, store, checkout = local_setup

    with open(logfile, "a") as f:
        f.write("ERROR one-off failure\nINFO next\n")
    handled = await daemon.poll_once()

    # A second sighting must not produce a second report.
    with open(logfile, "a") as f:
        f.write("ERROR one-off failure\nINFO next\n")
    assert await daemon.poll_once() == []
    assert len(handled) == 1


async def test_local_mode_notice_says_report_not_pr(local_setup):
    daemon, logfile, agent, store, checkout = local_setup
    notices = []
    daemon.on_notice = lambda message, level: notices.append((message, level))

    with open(logfile, "a") as f:
        f.write("ERROR boom\nINFO next\n")
    await daemon.poll_once()

    assert any("Report written" in message for message, _ in notices)
    assert not any("PR opened" in message for message, _ in notices)


async def test_local_mode_aclose_survives_no_github_client(local_setup):
    daemon, logfile, agent, store, checkout = local_setup
    await daemon.aclose()  # must not blow up on github=None


# ---------------------------------------------------------------------------
# Daily spend cap
# ---------------------------------------------------------------------------


def _seed_spend(store, fingerprint: str, cost: float) -> None:
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
    _seed_spend(store, "earlier", 0.02)

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
    _seed_spend(store, "earlier", 0.02)

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
    _seed_spend(store, "earlier", 0.05)
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


async def test_no_cap_by_default(setup):
    daemon, logfile, agent, github, store, remote = setup
    _seed_spend(store, "earlier", 999.0)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    assert len(await daemon.poll_once()) == 1


async def test_dry_run_ignores_the_cap(setup):
    """--dry-run costs money too, but it's an explicit interactive request."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.01
    _seed_spend(store, "earlier", 0.02)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once(dry_run=True)

    assert agent.prompts  # the analysis still ran


# ---------------------------------------------------------------------------
# Fix verification
# ---------------------------------------------------------------------------


def _fix_mode(daemon, *, test_command: str = "") -> None:
    repo_config = daemon.monitor_to_repo[daemon.monitors[0].name]
    repo_config.mode = "fix"
    repo_config.test_command = test_command


async def test_passing_tests_are_reported_in_the_pr(setup):
    daemon, logfile, agent, github, store, remote = setup
    _fix_mode(daemon, test_command="exit 0")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    body = github.calls[0]["body"]
    assert "Tests pass" in body
    assert "exit 0" in body


async def test_failing_tests_are_reported_not_suppressed(setup):
    """A fix that breaks the suite is exactly what a reviewer must be told."""
    daemon, logfile, agent, github, store, remote = setup
    _fix_mode(daemon, test_command="echo 'boom: 1 failed'; exit 1")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1  # the PR still opens
    body = github.calls[0]["body"]
    assert "Tests fail" in body
    assert "exit 1" in body
    assert "boom: 1 failed" in body


async def test_unrunnable_test_command_does_not_abort_the_incident(setup):
    daemon, logfile, agent, github, store, remote = setup
    _fix_mode(daemon, test_command="this-command-does-not-exist-xyz")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert "Tests fail" in github.calls[0]["body"]  # non-zero exit from the shell


async def test_no_test_command_marks_the_pr_unverified(setup):
    daemon, logfile, agent, github, store, remote = setup
    _fix_mode(daemon)  # no test_command

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Unverified" in github.calls[0]["body"]


async def test_tests_run_in_the_workspace_not_the_cwd(setup):
    """The command must see the agent's edits, which live in the clone."""
    daemon, logfile, agent, github, store, remote = setup
    _fix_mode(daemon, test_command="pwd")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    workspace = daemon.workspaces["owner/name"]
    assert str(workspace.path) in github.calls[0]["body"]


async def test_suggest_mode_does_not_run_tests(setup):
    """There is no diff to verify, so don't spend the time."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.monitor_to_repo[daemon.monitors[0].name]
    repo_config.test_command = "echo SHOULD_NOT_RUN"
    phases: list[str] = []
    daemon.progress = phases.append

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Running tests" not in phases
    assert "SHOULD_NOT_RUN" not in github.issues[0]["body"]


async def test_cap_warning_reports_the_configured_amount(setup):
    """A sub-cent cap must not be rounded up in the warning."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.005
    _seed_spend(store, "earlier", 0.02)
    notices: list[tuple[str, str]] = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    warning = next(msg for lvl, msg in notices if lvl == "warn")
    assert "$0.005" in warning
    assert "$0.01" not in warning
