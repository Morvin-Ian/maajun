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
        self.closed = False

    async def create_pull_request(self, repo, *, head, base, title, body):
        self.calls.append(
            {"repo": repo, "head": head, "base": base, "title": title, "body": body}
        )
        return f"https://github.com/{repo}/pull/{len(self.calls)}"

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


async def test_error_becomes_pull_request(setup):
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

    # PR was opened from the incident branch with the report in the body
    assert len(github.calls) == 1
    call = github.calls[0]
    assert call["head"] == f"maajun/incident-{fp}"
    assert call["base"] == "main"
    assert REPORT.splitlines()[0].lstrip("# ") in call["title"] or "IndexError" in call["title"]
    assert "Root cause" in call["body"]
    assert fp in call["body"]

    # Branch with the committed report exists on the remote
    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:docs/incidents/{fp}.md"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.returncode == 0
    assert "Root cause" in show.stdout

    # Incident recorded
    row = store.get(fp)
    assert row["status"] == "processed"
    assert row["pr_url"] == "https://github.com/owner/name/pull/1"
    assert row["branch"] == f"maajun/incident-{fp}"


async def test_same_error_does_not_open_second_pr(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert len(github.calls) == 1


async def test_failed_incident_is_marked_and_loop_survives(setup):
    daemon, logfile, agent, github, store, remote = setup

    async def boom(*args, **kwargs):
        raise RuntimeError("github down")

    github.create_pull_request = boom

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

    pr_url = await daemon.handle_manual_report(
        "Checkout button does nothing", repo_config, progress=phases.append
    )

    assert phases == ["Preparing workspace", "Analyzing with AI", "Opening PR"]
    assert pr_url.endswith("/pull/1")
    assert "Checkout button" in agent.prompts[0]
    assert github.calls[0]["head"].startswith("maajun/report-")


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


async def test_notices_emitted_for_new_error_and_pr(setup):
    daemon, logfile, agent, github, store, remote = setup
    notices: list[tuple[str, str]] = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    levels = [lvl for lvl, _ in notices]
    assert "info" in levels  # new error detected
    assert "success" in levels  # PR opened
    assert any("PR opened" in msg for _, msg in notices)


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

    github.create_pull_request = boom

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
