"""One incident, from the prompt to the artifact.

The report the agent is asked for, the diff fix mode has to produce, the
verification and repair round, and where it all ends up: a pull request, an
issue, a follow-up issue, a local file, or nothing at all.
"""

import subprocess
from pathlib import Path

import pytest

from daemon.fakes import (
    REPORT,
    TRACEBACK,
    FakeAgent,
    FakeGitHub,
    fix_mode,
    git,
)
from maajun.config import (
    Config,
    DaemonConfig,
    DeploymentConfig,
    GitHubConfig,
    MonitorConfig,
    RepoConfig,
)
from maajun.daemon.core import Daemon, LocalWorkspace
from maajun.daemon.investigation import blames_our_edits
from maajun.daemon.store import ARTIFACT_ISSUE, ARTIFACT_PR, IncidentStore
from maajun.monitors import ErrorEvent, LogFileMonitor
from maajun.providers.base import CompletionResponse
from maajun.vcs import CommandResult, GitWorkspace


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
    assert store.get(fp, "owner/name")["pr_url"].endswith("/issues/1")
    assert store.get(fp, "owner/name")["branch"] == ""
    # The incident is filed under the repo it was attributed to, not globally.
    assert store.get(fp) is None


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
    fix_mode(daemon, agent)

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

    row = store.get(fp, "owner/name")
    assert row["status"] == "processed"
    assert row["pr_url"] == "https://github.com/owner/name/pull/1"
    assert row["branch"] == f"maajun/incident-{fp}"


async def test_inactive_deployment_config_is_withheld_as_an_issue(setup):
    daemon, logfile, author, github, store, remote = setup
    fix_mode(daemon, author)
    repo = daemon.repo_for(daemon.monitors[0])
    repo.deployment = DeploymentConfig(
        service_command="{ path=/srv/app/.venv/bin/uvicorn ; argv[]=uvicorn app:api }",
        proxy_config_path="/etc/nginx/sites-available/api.example.com",
        proxy_body_limit="1m (nginx default; no active directive found)",
        config_owner="operator",
    )
    author.edit_path = daemon.workspaces[repo.repo].path / "nginx.conf"
    first_critic = FakeAgent("PASS")
    final_critic = FakeAgent(
        "BLOCK\n"
        "Issue title: Raise the active nginx request-body limit\n"
        "The operator-owned proxy still rejects the request."
    )
    agents = iter((author, first_critic, final_critic))
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: next(agents)

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    await daemon.poll_once()

    assert github.calls == []
    assert len(github.issues) == 1
    assert github.issues[0]["repo"] == "owner/name"
    assert github.issues[0]["title"] == (
        "[maajun] Raise the active nginx request-body limit"
    )
    assert "Fix publication withheld" in github.issues[0]["body"]
    assert "Fix PR withheld" in github.issues[0]["body"]
    assert "No code change" not in github.issues[0]["body"]
    assert "Draft repository change (not published)" in github.issues[0]["body"]
    assert "/etc/nginx/sites-available/api.example.com" in github.issues[0]["body"]
    assert "nginx.conf" in github.issues[0]["body"]
    assert str(daemon.workspaces[repo.repo].path) in final_critic.prompts[0]
    assert "deployment folder is runtime evidence" in final_critic.prompts[0]
    assert "Active proxy request-body limit: 1m" in final_critic.prompts[0]


async def test_quality_correction_reruns_owner_verification(setup):
    daemon, logfile, author, github, store, remote = setup
    fix_mode(daemon, author, test_command="true")
    repo = daemon.repo_for(daemon.monitors[0])
    repo.deployment.service_command = "/usr/bin/python -m app"
    repo.verification_commands = ["printf verification"]
    workspace = daemon.workspaces[repo.repo]
    original_run = workspace.run_command
    commands: list[str] = []

    async def record_run(command, **kwargs):
        commands.append(command)
        return await original_run(command, **kwargs)

    workspace.run_command = record_run
    first_critic = FakeAgent(
        "BLOCK\nIssue title: Add upload boundary coverage\n"
        "The behavior boundary needs a regression test."
    )
    final_critic = FakeAgent("PASS")
    agents = iter((author, first_critic, final_critic))
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: next(agents)

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    await daemon.poll_once()

    assert commands == [
        "true",
        "printf verification",
        "true",
        "printf verification",
    ]
    assert len(github.calls) == 1
    assert github.issues == []
    assert "Owner-controlled verification results" in final_critic.prompts[0]
    assert "Tests pass" in final_critic.prompts[0]


async def test_related_verification_failure_withholds_the_fix(setup):
    daemon, logfile, author, github, store, remote = setup
    command = "echo 'main.py import failed'; exit 1"
    fix_mode(daemon, author, test_command=command)
    repo = daemon.repo_for(daemon.monitors[0])
    repo.deployment.service_command = "/usr/bin/python -m app"
    first_critic = FakeAgent("PASS")
    final_critic = FakeAgent("PASS")
    agents = iter((author, first_critic, final_critic))
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: next(agents)

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    await daemon.poll_once()

    assert github.calls == []
    assert len(github.issues) == 1
    assert github.issues[0]["repo"] == "owner/name"
    assert "Fix publication withheld" in github.issues[0]["body"]
    assert "still fails after the repair round" in github.issues[0]["body"]
    assert command in first_critic.prompts[0]
    assert command in final_critic.prompts[0]
    assert len(author.prompts) == 3


async def test_failed_quality_correction_withholds_the_fix(setup):
    daemon, logfile, author, github, store, remote = setup

    class DyingCorrection(FakeAgent):
        async def chat(self, message):
            if self.prompts:
                self.prompts.append(message)
                raise RuntimeError("provider unavailable")
            return await super().chat(message)

    dying = DyingCorrection()
    fix_mode(daemon, dying)
    repo = daemon.repo_for(daemon.monitors[0])
    repo.deployment.service_command = "/usr/bin/python -m app"
    critic = FakeAgent(
        "BLOCK\nIssue title: Add an upload boundary test\n"
        "The regression test does not exercise the request boundary."
    )
    agents = iter((dying, critic))
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: next(agents)

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    await daemon.poll_once()

    assert github.calls == []
    assert len(github.issues) == 1
    assert "Fix publication withheld" in github.issues[0]["body"]
    assert "one allowed correction could not run" in github.issues[0]["body"]
    assert len(dying.prompts) == 2


class SpendingAgent:
    """Fails partway, having already paid for the rounds it did make.

    Mirrors the real Agent: usage accumulates across tool rounds and is read
    back with take_usage(), not off a response that a failed turn never
    produced.
    """

    model = "deepseek-v4-flash"

    def __init__(self, usage=None):
        # `is None`, not `or`: an empty dict is a real case — a turn that died
        # before the provider reported anything.
        self.usage = (
            {"prompt_tokens": 500_000, "completion_tokens": 100_000}
            if usage is None
            else usage
        )
        self.closed = False

    async def chat(self, message):
        raise RuntimeError("provider gave up on round 30")

    def take_usage(self):
        usage, self.usage = self.usage, {}
        return usage

    async def aclose(self):
        self.closed = True


async def test_a_failed_analysis_still_banks_what_it_spent(setup):
    """Every tool round is a billed request. Reading the cost off the response
    drops all of it when there is no response, and the daily cap under-counts
    exactly the incidents that are retried hardest."""
    daemon, logfile, agent, github, store, remote = setup
    spender = SpendingAgent()
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: spender

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    row = store.all()[0]
    assert row["status"] == "failed"
    assert row["prompt_tokens"] == 500_000
    assert row["completion_tokens"] == 100_000
    assert row["cost_usd"] > 0
    assert store.cost_since("1970-01-01T00:00:00Z") == row["cost_usd"]
    assert spender.closed, "the HTTP client is still released on the failure path"


async def test_banked_spend_from_failures_reaches_the_cap(setup):
    """Three retries of a failing incident must eventually stop the daemon."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.config.daemon.max_usd_per_day = 0.05
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: SpendingAgent()

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert daemon.over_budget(), "a failed analysis is not a free one"


async def test_a_failure_before_the_first_response_banks_nothing(setup):
    daemon, logfile, agent, github, store, remote = setup
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: SpendingAgent(usage={})

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert store.all()[0]["cost_usd"] == 0


async def test_recording_the_spend_never_masks_the_original_failure(setup):
    """The incident is already failing; a store that also breaks must not
    swallow the exception that explains why."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: SpendingAgent()

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    store.add_spend = broken

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert store.all()[0]["status"] == "failed"


async def test_fix_mode_commits_agent_changes(setup):
    daemon, logfile, agent, github, store, remote = setup
    # Update the repo config mode to "fix"
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    workspace = daemon.workspaces["owner/name"]
    agent.edit_path = workspace.path / "main.py"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()
    fp = handled[0]

    assert "Now fix it." in agent.prompts[0]

    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:main.py"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.stdout == "items = [0]\n"
    assert "applied fix" in github.calls[0]["body"]


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
    repo_config = daemon.repo_for(daemon.monitors[0])
    phases: list[str] = []

    url = await daemon.handle_manual_report(
        "Checkout button does nothing", repo_config, progress=phases.append
    )

    assert phases == ["Preparing workspace", "Analyzing with AI", "Filing issue"]
    assert url.endswith("/issues/1")
    assert "Checkout button" in agent.prompts[0]
    # Titled from the report's finding, not from how the issue was described:
    # what the analysis says to fix is what the issue is called.
    assert github.issues[0]["title"] == "[maajun] IndexError in handler"


async def test_manual_report_dry_run_only_analyzes(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    phases: list[str] = []

    pr_url = await daemon.handle_manual_report(
        "Something is broken", repo_config, dry_run=True, progress=phases.append
    )

    assert pr_url == ""
    assert phases == ["Analyzing with AI"]  # no workspace prep / PR in dry run
    assert github.calls == []


# ---------------------------------------------------------------------------
# Local mode: no GitHub configured
# ---------------------------------------------------------------------------


@pytest.fixture
def local_setup(tmp_path):
    """A daemon with no GitHub repo — reports go to disk instead of PRs."""
    from maajun.daemon.core import LocalWorkspace

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
        monitor_to_repo={id(monitor): repo_config},
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


async def test_passing_tests_are_reported_in_the_pr(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent, test_command="exit 0")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    body = github.calls[0]["body"]
    assert "Tests pass" in body
    assert "exit 0" in body


async def test_failing_tests_are_reported_not_suppressed(setup):
    """A fix that breaks the suite is exactly what a reviewer must be told."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent, test_command="echo 'boom: 1 failed'; exit 1")

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
    fix_mode(daemon, agent, test_command="this-command-does-not-exist-xyz")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert "Tests fail" in github.calls[0]["body"]  # non-zero exit from the shell


async def test_no_test_command_marks_the_pr_unverified(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)  # no test_command

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Unverified" in github.calls[0]["body"]


async def test_tests_run_in_the_workspace_not_the_cwd(setup):
    """The command must see the agent's edits, which live in the clone."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent, test_command="pwd")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    workspace = daemon.workspaces["owner/name"]
    assert str(workspace.path) in github.calls[0]["body"]


async def test_suggest_mode_does_not_run_tests(setup):
    """There is no diff to verify, so don't spend the time."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.test_command = "echo SHOULD_NOT_RUN"
    phases: list[str] = []
    daemon.progress = phases.append

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Running tests" not in phases
    assert "SHOULD_NOT_RUN" not in github.issues[0]["body"]


async def test_reproduction_fails_before_the_edit_and_passes_after(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.reproduction_command = "grep -q '\\[0\\]' main.py"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    body = github.calls[0]["body"]
    assert "Before edit: reproduced" in body
    assert "After edit: no longer reproduces" in body


async def test_reproduction_and_post_fix_commands_run_in_documented_order(
    setup, tmp_path
):
    daemon, logfile, agent, github, store, remote = setup
    marker = tmp_path / "verification-order"
    fix_mode(daemon, agent, test_command=f"echo test >> {marker}")
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.reproduction_command = (
        f"if grep -Fq 'items = [0]' main.py; then echo repro-after >> {marker}; "
        f"else echo repro-before >> {marker}; exit 1; fi"
    )
    repo_config.verification_commands = [f"echo verify >> {marker}"]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert marker.read_text().splitlines() == [
        "repro-before", "repro-after", "test", "verify",
    ]


async def test_a_reproduction_timeout_is_reported_without_aborting(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.reproduction_command = "pytest -q tests/test_bug.py"
    workspace = daemon.workspaces[repo_config.repo]

    async def timed_out(command):
        return CommandResult(None, "Timed out after 600s.")

    workspace.run_command = timed_out

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    body = github.calls[0]["body"]
    assert "Before edit: timed out" in body
    assert "After edit: timed out" in body


async def test_every_post_fix_command_runs_even_after_a_failure(setup, tmp_path):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent, test_command="echo 'FAILED main.py'; exit 1")
    marker = tmp_path / "second-ran"
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.verification_commands = [f"touch {marker}"]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert marker.exists()
    body = github.calls[0]["body"]
    assert "Tests fail" in body
    assert f"`touch {marker}`" in body


async def test_legacy_and_additional_commands_are_deduplicated(setup, tmp_path):
    daemon, logfile, agent, github, store, remote = setup
    marker = tmp_path / "runs"
    command = f"echo run >> {marker}"
    fix_mode(daemon, agent, test_command=command)
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.verification_commands = [command]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert marker.read_text().splitlines() == ["run"]


async def test_a_still_failing_reproduction_earns_one_repair_without_a_filename(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    repo_config.reproduction_command = "echo still-broken; exit 1"
    agent.edit_path = daemon.workspaces[repo_config.repo].path / "main.py"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(agent.prompts) == 2
    assert "still-broken" in agent.prompts[1]
    assert "still reproduces" in github.calls[0]["body"]


async def test_repair_reruns_reproduction_and_every_post_fix_command(setup, tmp_path):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repaired = tmp_path / "repaired"
    runs = tmp_path / "verification-runs"

    class Repairing(FakeAgent):
        async def chat(self, message):
            response = await super().chat(message)
            if len(self.prompts) == 2:
                repaired.write_text("done")
            return response

    repairing = Repairing(
        edit_path=daemon.workspaces[repo_config.repo].path / "main.py"
    )
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: repairing
    repo_config.mode = "fix"
    repo_config.reproduction_command = f"test -f {repaired}"
    repo_config.test_command = f"echo test >> {runs}"
    repo_config.verification_commands = [f"echo verify >> {runs}"]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(repairing.prompts) == 2
    assert runs.read_text().splitlines() == ["test", "verify", "test", "verify"]
    assert "After edit: no longer reproduces" in github.calls[0]["body"]


# ---------------------------------------------------------------------------
# Deploy blame
# ---------------------------------------------------------------------------


async def test_prompt_describes_the_deployment(setup):
    """A 502 from a proxy and a worker timeout only make sense against how
    the app actually runs, which the clone cannot show."""
    daemon, logfile, agent, github, store, remote = setup
    deployment = daemon.repo_configs[0].deployment
    deployment.path = "/srv/kfl"
    deployment.port = 8000
    deployment.runs = "docker compose"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    prompt = agent.prompts[0]
    assert "Folder on the server: /srv/kfl" in prompt
    assert "Listens on port: 8000" in prompt
    assert "Started by: docker compose" in prompt
    assert f"Errors are read from: logfile:{logfile}" in prompt


def test_nothing_recorded_omits_the_deployment_section():
    """Better silent than a report that says "port 0"."""
    from maajun.daemon.investigation import deployment_section

    assert deployment_section(RepoConfig(repo="acme/api"), []) == ""


def test_the_deployment_section_lists_only_what_is_known():
    from maajun.daemon.investigation import deployment_section

    section = deployment_section(
        RepoConfig(repo="acme/api", deployment=DeploymentConfig(port=8000)),
        ["docker:api-web-1"],
    )

    assert "Listens on port: 8000" in section
    assert "Errors are read from: docker:api-web-1" in section
    assert "Folder on the server" not in section


async def test_prompt_offers_recent_commits_for_blame(setup):
    """The clone is already on disk; naming the likely commit is nearly free."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    prompt = agent.prompts[0]
    assert "Likely cause commit" in prompt
    assert "Recent commits on main" in prompt
    assert "initial" in prompt  # the seed commit's subject


async def test_local_mode_without_git_history_omits_the_section(tmp_path):
    """A plain directory has no commits; don't invite the model to invent one."""
    from maajun.daemon.core import LocalWorkspace

    logfile = tmp_path / "app.log"
    logfile.write_text("")
    checkout = tmp_path / "plain"
    checkout.mkdir()

    repo_config = RepoConfig(mode="suggest")
    config = Config(
        monitor=MonitorConfig(log_files=[str(logfile)]),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    agent = FakeAgent()
    monitor = LogFileMonitor(logfile)
    daemon = Daemon(
        config,
        monitors=[monitor],
        store=IncidentStore(tmp_path / "work" / "i.db"),
        workspaces={"": LocalWorkspace(checkout)},
        monitor_to_repo={id(monitor): repo_config},
        github=None,
        agent_factory_for_repo=lambda rc, ws: lambda: agent,
        repo_configs=[repo_config],
        report_dir=tmp_path / "work" / "reports",
        local_mode=True,
    )

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Recent commits" not in agent.prompts[0]


async def test_unreadable_git_history_does_not_break_the_incident(setup):
    """History is a nice-to-have; failing to read it must not abort analysis."""
    daemon, logfile, agent, github, store, remote = setup
    workspace = daemon.workspaces["owner/name"]

    async def boom(limit=0):
        raise RuntimeError("git exploded")

    workspace.recent_commits = boom

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert "Recent commits" not in agent.prompts[0]


# ---------------------------------------------------------------------------
# Recording the analysis itself
# ---------------------------------------------------------------------------


async def test_suggest_mode_records_the_report_and_marks_it_an_issue(setup):
    """The analysis used to exist only in the GitHub issue; chat recalls it."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    row = store.get(fp, "owner/name")
    assert row["artifact_kind"] == ARTIFACT_ISSUE
    assert "Root cause" in row["report_text"]


async def test_fix_mode_records_the_report_and_marks_it_a_pr(tmp_path, remote):
    daemon, logfile, store = fix_mode_daemon(tmp_path, remote)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    row = store.get(fp, "owner/name")
    assert row["artifact_kind"] == ARTIFACT_PR
    assert "Root cause" in row["report_text"]


def fix_mode_daemon(tmp_path, remote):
    logfile = tmp_path / "app.log"
    logfile.write_text("")
    repo_config = RepoConfig(repo="owner/name", base_branch="main", mode="fix")
    config = Config(
        github=GitHubConfig(repos=[repo_config]),
        monitor=MonitorConfig(log_files=[str(logfile)], poll_interval=1),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    workspace = GitWorkspace(
        tmp_path / "work" / "ws", "owner/name", remote_url=str(remote)
    )
    store = IncidentStore(tmp_path / "work" / "incidents.db")
    monitor = LogFileMonitor(logfile)
    daemon = Daemon(
        config,
        monitors=[monitor],
        store=store,
        workspaces={"owner/name": workspace},
        monitor_to_repo={id(monitor): repo_config},
        github=FakeGitHub(),
        agent_factory_for_repo=lambda rc, ws: lambda: FakeAgent(
            edit_path=ws.path / "main.py"
        ),
    )
    return daemon, logfile, store


# ---------------------------------------------------------------------------
# Fix mode that changes nothing
# ---------------------------------------------------------------------------


async def test_fix_mode_with_no_diff_files_an_issue_not_an_empty_pull_request(setup):
    """Fix mode used to open a PR either way, on the reasoning that the
    committed report file was itself a diff to review. In practice that
    shipped pull requests that look like fixes until you open the Files tab.
    Asked twice and still nothing to merge means the finding is a finding,
    and a finding is an issue.
    """
    daemon, logfile, agent, github, store, remote = setup
    daemon.repo_for(daemon.monitors[0]).mode = "fix"  # agent edits nothing

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    assert github.calls == []
    assert len(github.issues) == 1
    row = store.get(fp, "owner/name")
    assert row["artifact_kind"] == ARTIFACT_ISSUE
    assert row["branch"] == ""


async def test_an_issue_from_fix_mode_says_the_fix_was_attempted(setup):
    """Otherwise it reads as suggest mode, and nobody knows an edit was
    tried and found unnecessary."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.repo_for(daemon.monitors[0]).mode = "fix"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    body = github.issues[0]["body"]
    assert "No code change" in body
    assert "Root cause" in body  # the analysis is still there


async def test_an_issue_from_suggest_mode_claims_no_attempt(setup):
    daemon, logfile, agent, github, store, remote = setup
    assert daemon.repo_for(daemon.monitors[0]).mode == "suggest"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "No code change" not in github.issues[0]["body"]


async def test_a_pull_request_with_a_fix_does_not_say_analysis_only(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Analysis only" not in github.calls[0]["body"]


async def test_no_branch_is_pushed_when_there_is_nothing_to_merge(setup):
    """An orphan branch per unfixable incident is litter in the repo."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.repo_for(daemon.monitors[0]).mode = "fix"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    branches = subprocess.run(
        ["git", "branch", "-a"], cwd=str(remote),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "maajun/incident-" not in branches


async def test_a_branch_is_pushed_when_there_is_a_fix(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    branches = subprocess.run(
        ["git", "branch", "-a"], cwd=str(remote),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "maajun/incident-" in branches


async def test_an_earlier_runs_edits_are_not_this_incidents_fix(setup):
    """One clone serves every incident. A run that died after the agent had
    edited files used to leave them on the tree, where the next incident read
    them as its own fix and opened a pull request from someone else's diff.
    """
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    repo_config = daemon.repo_for(daemon.monitors[0])
    workspace = daemon.workspaces["owner/name"]
    # An unusable report twice over: the run raises after the edit is made.
    agent.replies = ["no.", "still no."]

    first = ErrorEvent(source="log", message="IndexError", details=TRACEBACK)
    with pytest.raises(RuntimeError):
        await daemon.handle_incident(first, repo_config, workspace)
    assert await workspace.has_changes()  # the dead run's edit, still there

    # A different error, and this time the agent changes nothing.
    agent.replies = None
    agent.edit_path = None
    second = ErrorEvent(
        source="log", message="KeyError", details="KeyError: 'promotion'"
    )
    await daemon.handle_incident(second, repo_config, workspace)

    assert github.calls == []
    assert len(github.issues) == 1


async def test_a_branch_carrying_only_the_report_is_not_pushed(setup):
    """The report file is committed on the branch, so `git status` alone
    cannot answer whether this run fixed anything."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    agent.edit_path = (
        daemon.workspaces["owner/name"].path / "docs" / "incidents" / "notes.md"
    )

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    assert github.calls == []
    assert store.get(fp, "owner/name")["artifact_kind"] == ARTIFACT_ISSUE


async def test_the_issue_a_tripped_gate_files_keeps_the_follow_up(setup):
    """The follow-up is split off for the pull request's sake. With no pull
    request to open, dropping it would lose work nobody has recorded."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    agent.report = FIXED_WITH_FOLLOW_UP
    agent.edit_path = (
        daemon.workspaces["owner/name"].path / "docs" / "incidents" / "notes.md"
    )

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "## Follow-up" in github.issues[0]["body"]


async def test_the_notice_says_pr_when_one_was_opened(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    notices = []
    daemon.on_notice = lambda message, level: notices.append(message)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert any("PR opened" in n for n in notices)


# ---------------------------------------------------------------------------
# Local mode
# ---------------------------------------------------------------------------


def local_daemon(tmp_path, repo_path):
    logfile = tmp_path / "app.log"
    logfile.write_text("")
    config = Config(
        monitor=MonitorConfig(log_files=[str(logfile)], poll_interval=1),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    store = IncidentStore(tmp_path / "work" / "incidents.db")
    monitor = LogFileMonitor(logfile)
    repo_config = RepoConfig(mode="suggest")
    agent = FakeAgent()
    daemon = Daemon(
        config,
        monitors=[monitor],
        store=store,
        workspaces={"": LocalWorkspace(repo_path)},
        monitor_to_repo={id(monitor): repo_config},
        github=None,
        agent_factory_for_repo=lambda rc, ws: lambda: agent,
        repo_configs=[repo_config],
        local_mode=True,
    )
    return daemon, logfile, agent, store


async def test_local_mode_offers_commit_history_for_deploy_blame(tmp_path):
    """LocalWorkspace had no recent_commits, so the section was always skipped.

    The daemon probes for the method with getattr; without it, every local
    report's "Likely cause commit" was "Unclear" even in a git checkout.
    """
    checkout = tmp_path / "app"
    checkout.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(checkout)],
                   check=True, capture_output=True)
    (checkout / "main.py").write_text("x = 1\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-m", "the suspicious commit", cwd=checkout)

    daemon, logfile, agent, store = local_daemon(tmp_path, checkout)
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "the suspicious commit" in agent.prompts[0]


async def test_local_mode_does_not_name_a_branch_it_cannot_vouch_for(tmp_path):
    """Nothing pinned the checkout to base_branch, so 'main' would be a guess."""
    checkout = tmp_path / "app"
    checkout.mkdir()
    subprocess.run(["git", "init", "-b", "wip", str(checkout)],
                   check=True, capture_output=True)
    (checkout / "main.py").write_text("x = 1\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-m", "only commit", cwd=checkout)

    daemon, logfile, agent, store = local_daemon(tmp_path, checkout)
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "the checked-out branch" in agent.prompts[0]


async def test_a_local_directory_that_is_not_a_repo_omits_the_section(tmp_path):
    """No history to offer beats the model inventing a commit."""
    plain = tmp_path / "plain"
    plain.mkdir()

    daemon, logfile, agent, store = local_daemon(tmp_path, plain)
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "Recent commits" not in agent.prompts[0]


async def test_local_mode_still_writes_its_report(tmp_path):
    """The blame lookup must not disturb the artifact."""
    checkout = tmp_path / "app"
    checkout.mkdir()

    daemon, logfile, agent, store = local_daemon(tmp_path, checkout)
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    row = store.get(fp, "")
    assert row["artifact_kind"] == "report"
    assert Path(row["pr_url"]).exists()


# ---------------------------------------------------------------------------
# Nothing is published without content
# ---------------------------------------------------------------------------


async def test_an_empty_answer_is_re_asked_before_anything_is_filed(setup):
    """A model that answers conversationally gets one more round, rather than
    an empty issue standing in for a finding."""
    daemon, logfile, agent, github, store, remote = setup
    agent.replies = ["Sure! Let me take a look.", REPORT]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    assert len(agent.prompts) == 2
    assert "not a usable report" in agent.prompts[1]
    assert len(github.issues) == 1
    assert "Root cause" in store.get(fp, "owner/name")["report_text"]


async def test_a_re_ask_does_not_lose_the_first_asks_tokens(setup):
    """chat() reports one call, and the first is the expensive one — counting
    only the retry would under-report the cost and under-spend the cap."""
    daemon, logfile, agent, github, store, remote = setup
    agent.replies = ["Sure! Let me look.", REPORT]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    row = store.get(fp, "owner/name")
    assert row["prompt_tokens"] == 2000  # both asks, not just the second
    assert row["completion_tokens"] == 200


async def test_a_failed_run_banks_what_every_ask_cost(setup):
    daemon, logfile, agent, github, store, remote = setup
    agent.replies = ["nope", "still nope"]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    row = store.all()[0]
    assert row["status"] == "failed"
    assert row["prompt_tokens"] == 2000


async def test_a_report_that_stays_empty_files_nothing(setup):
    """Better a failed incident, visible in `maajun incidents`, than an issue
    that tells the reader nothing."""
    daemon, logfile, agent, github, store, remote = setup
    agent.replies = ["Sure!", "Still nothing useful."]
    notices = []
    daemon.on_notice = lambda message, level: notices.append((level, message))

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert github.issues == [] and github.calls == []
    assert any(level == "error" for level, _ in notices)
    assert [row["status"] for row in store.all()] == ["failed"]


@pytest.mark.parametrize("report,problem", [
    ("", "empty"),
    ("Looks fine to me.", "characters long"),
    ("x" * 400, "none of the report's sections"),
])
def test_what_counts_as_an_unusable_report(report, problem):
    from maajun.daemon.reports import report_problem

    assert problem in report_problem(report)


def test_a_filled_in_report_passes():
    from maajun.daemon.reports import report_problem

    assert report_problem(REPORT) == ""


# ---------------------------------------------------------------------------
# Manual reports are incidents too
# ---------------------------------------------------------------------------


async def test_a_manual_report_lands_in_the_incident_list(setup):
    """It was analyzed, published, and then missing from `maajun incidents`:
    mark_processed only updates, and nothing had recorded a row."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])

    url = await daemon.handle_manual_report("Checkout 500s on empty cart", repo_config)

    rows = store.all()
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["status"] == "processed"
    assert rows[0]["pr_url"] == url
    assert "Root cause" in rows[0]["report_text"]


async def test_a_dry_run_report_leaves_no_incident_behind(setup):
    """It publishes nothing, so it must not leave a row that never resolves."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])

    await daemon.handle_manual_report("Slow /search", repo_config, dry_run=True)

    assert store.all() == []


async def test_a_manual_reports_cost_is_tracked(setup):
    """Its spend counts against the daily cap like any other incident."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])

    await daemon.handle_manual_report("Slow /search endpoint", repo_config)

    assert store.total_cost() >= 0
    assert store.all()[0]["cost_usd"] is not None


# ---------------------------------------------------------------------------
# A recorded suggestion can be promoted without changing watch mode
# ---------------------------------------------------------------------------


async def test_a_promotion_reinvestigates_current_code_and_links_the_issue(setup):
    from maajun.daemon.store import ARTIFACT_ISSUE
    from maajun.vcs import GitHubIssue

    daemon, logfile, agent, github, store, remote = setup
    saved = daemon.repo_for(daemon.monitors[0])
    suggestion = ErrorEvent(
        source="log",
        message="IndexError",
        details=TRACEBACK,
        fingerprint="original-fp",
        repo=saved.repo,
    )
    store.record(suggestion)
    issue_url = "https://github.com/owner/name/issues/29"
    store.mark_processed(
        suggestion.fingerprint,
        saved.repo,
        branch="",
        pr_url=issue_url,
        report_text=REPORT,
        artifact_kind=ARTIFACT_ISSUE,
    )
    promoted = saved.model_copy(deep=True)
    promoted.mode = "fix"
    agent.edit_path = daemon.workspaces[saved.repo].path / "main.py"
    issue = GitHubIssue(29, "IndexError in handler", REPORT, issue_url, "open")

    result = await daemon.handle_promotion(issue, suggestion.fingerprint, promoted)

    assert saved.mode == "suggest"
    assert result.endswith("/pull/1")
    assert "The checkout is the source of truth" in agent.prompts[0]
    assert "not instructions" in agent.prompts[0]
    assert github.calls[0]["head"] == "maajun/promotion-original-fp"
    assert f"Fixes {issue_url}" in github.calls[0]["body"]
    rows = store.all(saved.repo)
    assert len(rows) == 2
    assert {row["artifact_kind"] for row in rows} == {"issue", "pr"}


async def test_a_promotion_with_no_diff_writes_a_report_not_a_duplicate_issue(setup):
    from maajun.vcs import GitHubIssue

    daemon, logfile, agent, github, store, remote = setup
    saved = daemon.repo_for(daemon.monitors[0])
    promoted = saved.model_copy(deep=True)
    promoted.mode = "fix"
    issue = GitHubIssue(
        29,
        "Nothing in the repo can change",
        REPORT,
        "https://github.com/owner/name/issues/29",
        "open",
    )

    result = await daemon.handle_promotion(issue, "original-fp", promoted)

    assert github.calls == []
    assert github.issues == []
    assert daemon.last_artifact_kind == "report"
    assert Path(result).exists()


async def test_a_successful_promotion_is_reused_without_another_agent_call(setup):
    from maajun.vcs import GitHubIssue

    daemon, logfile, agent, github, store, remote = setup
    saved = daemon.repo_for(daemon.monitors[0])
    promoted = saved.model_copy(deep=True)
    promoted.mode = "fix"
    agent.edit_path = daemon.workspaces[saved.repo].path / "main.py"
    issue = GitHubIssue(
        29,
        "IndexError in handler",
        REPORT,
        "https://github.com/owner/name/issues/29",
        "open",
    )

    first = await daemon.handle_promotion(issue, "original-fp", promoted)
    prompt_count = len(agent.prompts)
    second = await daemon.handle_promotion(issue, "original-fp", promoted)

    assert second == first
    assert len(agent.prompts) == prompt_count


async def test_a_promotion_dry_run_refreshes_code_without_a_branch_or_record(setup):
    from maajun.vcs import GitHubIssue

    daemon, logfile, agent, github, store, remote = setup
    saved = daemon.repo_for(daemon.monitors[0])
    promoted = saved.model_copy(deep=True)
    promoted.mode = "fix"
    issue = GitHubIssue(
        29,
        "IndexError in handler",
        REPORT,
        "https://github.com/owner/name/issues/29",
        "open",
    )
    phases = []
    agent_modes = []
    original_factory = daemon.agent_factory_for_repo

    def record_mode(repo_config, workspace):
        agent_modes.append(repo_config.mode)
        return original_factory(repo_config, workspace)

    daemon.agent_factory_for_repo = record_mode

    result = await daemon.handle_promotion(
        issue, "original-fp", promoted, dry_run=True, progress=phases.append
    )

    assert result == ""
    assert "Preparing workspace" in phases
    assert github.calls == [] and github.issues == []
    assert store.all(saved.repo) == []
    branches = subprocess.run(
        ["git", "branch", "--list", "maajun/promotion-original-fp"],
        cwd=daemon.workspaces[saved.repo].path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""
    assert "## Suggested fix" in agent.prompts[0]
    assert "Now fix it" not in agent.prompts[0]
    assert agent_modes == ["suggest"]


async def test_a_by_design_promotion_dry_run_leaves_no_record_or_report(setup):
    from maajun.vcs import GitHubIssue

    daemon, logfile, agent, github, store, remote = setup
    saved = daemon.repo_for(daemon.monitors[0])
    promoted = saved.model_copy(deep=True)
    promoted.mode = "fix"
    agent.report = """# the guard rejects an invalid request

## Verdict
by design — the validation guard intentionally returns a 400 response.

## What happened
An invalid request was rejected.

## Root cause
None. `main.py:1` implements the documented validation rule.

## Suggested fix
None — working as intended.
"""
    issue = GitHubIssue(
        29,
        "Expected validation response",
        agent.report,
        "https://github.com/owner/name/issues/29",
        "open",
    )

    result = await daemon.handle_promotion(issue, "original-fp", promoted, dry_run=True)

    assert result == ""
    assert store.all(saved.repo) == []
    assert list(daemon.report_dir.glob("*.md")) == []


# ---------------------------------------------------------------------------
# A bug that comes back
# ---------------------------------------------------------------------------


async def test_a_returning_error_is_filed_again_and_says_so(setup):
    """The reader's first question is whether this is new; the second is what
    was filed last time."""
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]
    store.conn.execute("UPDATE incidents SET last_seen = '2020-01-01T00:00:00+00:00'")
    store.conn.commit()

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == [fp]
    assert len(github.issues) == 2
    body = github.issues[1]["body"]
    assert "reported before and has come back" in body
    assert github.issues[0]["body"].count("come back") == 0


async def test_the_agent_is_told_the_fix_did_not_hold(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()
    store.conn.execute("UPDATE incidents SET last_seen = '2020-01-01T00:00:00+00:00'")
    store.conn.commit()

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert "reported before" in agent.prompts[1]
    assert "reported before" not in agent.prompts[0]


async def test_an_error_that_never_stopped_is_not_filed_twice(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()
    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert handled == []
    assert len(github.issues) == 1


# ---------------------------------------------------------------------------
# Fix mode has to actually fix
# ---------------------------------------------------------------------------

DESCRIBED_BUT_NOT_APPLIED = """# settings inherit a wildcard in production

## Root cause
`config/settings/base.py:58` falls back to "*" when the env var is unset.

## Suggested fix
Pin it in production.py.

## Applied fix
Not yet applied — this report documents the gap and the concrete patch.
"""


async def test_fix_mode_that_only_described_the_fix_is_asked_again(setup, tmp_path):
    """A pull request with no diff publishes nothing anyone can review. The
    escape hatch for fixes that live outside the repo gets taken for findings
    that do have an in-repo fix — an environment variable especially."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    workspace = daemon.workspaces["owner/name"]
    edit = workspace.path / "main.py"

    class Reluctant(FakeAgent):
        """Describes the fix, then applies it only when pushed."""

        async def chat(self, message):
            self.prompts.append(message)
            if len(self.prompts) > 1:
                edit.write_text("items = [0]\n")
                return CompletionResponse(
                    content=REPORT, usage=dict(self.usage_per_call)
                )
            return CompletionResponse(
                content=DESCRIBED_BUT_NOT_APPLIED, usage=dict(self.usage_per_call)
            )

    reluctant = Reluctant()
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: reluctant

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(reluctant.prompts) == 2
    assert "You changed no files" in reluctant.prompts[1]
    assert edit.read_text() == "items = [0]\n"
    # A real diff, so the PR is a fix rather than an analysis.
    assert len(github.calls) == 1
    assert "Analysis only" not in github.calls[0]["body"]


async def test_a_fix_that_landed_first_time_is_not_asked_twice(setup, tmp_path):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    workspace = daemon.workspaces["owner/name"]
    edits = FakeAgent(edit_path=workspace.path / "main.py")
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: edits

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(edits.prompts) == 1


async def test_the_second_ask_keeps_the_first_report_when_it_answers_badly(setup):
    """A model that edits the files and replies "done" must not cost the
    analysis that came with the first answer."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    workspace = daemon.workspaces["owner/name"]

    class Terse(FakeAgent):
        async def chat(self, message):
            self.prompts.append(message)
            if len(self.prompts) > 1:
                (workspace.path / "main.py").write_text("items = [0]\n")
                return CompletionResponse(content="Done.", usage={})
            return CompletionResponse(
                content=DESCRIBED_BUT_NOT_APPLIED, usage=dict(self.usage_per_call)
            )

    terse = Terse()
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: terse

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    body = github.calls[0]["body"]
    assert "Root cause" in body
    assert "Done." not in body


async def test_suggest_mode_is_never_asked_for_an_edit(setup):
    """It has no branch and no write permission; asking would be nonsense."""
    daemon, logfile, agent, github, store, remote = setup
    assert daemon.repo_for(daemon.monitors[0]).mode == "suggest"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(agent.prompts) == 1
    assert len(github.issues) == 1


# ---------------------------------------------------------------------------
# The report's own diff gets applied
# ---------------------------------------------------------------------------

# What a model leaves behind when it describes the fix but never calls
# edit_file: the exact patch, sitting in the report.
REPORT_WITH_PATCH = REPORT + """

## Applied fix
main.py guards the access:

```diff
--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-items = []
+items = [0]
```
"""

REPORT_WITH_STALE_PATCH = REPORT + """

## Applied fix
main.py is untouched — the change belongs in services:

```diff
--- a/services/cart.py
+++ b/services/cart.py
@@ -1 +1 @@
-items = []
+items = [0]
```
"""


async def test_a_report_that_carries_the_diff_gets_it_applied(setup):
    """Twice asked, the model still only described the change — but the patch
    was in the report. Applying it costs no third round, and the run ships a
    PR with a real diff instead of downgrading to an issue."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.repo_for(daemon.monitors[0]).mode = "fix"

    class Describer(FakeAgent):
        """Never edits; describes first, then hands over the patch."""

        async def chat(self, message):
            self.prompts.append(message)
            return CompletionResponse(
                content=self.replies.pop(0), usage=dict(self.usage_per_call)
            )

    describer = Describer()
    describer.replies = [DESCRIBED_BUT_NOT_APPLIED, REPORT_WITH_PATCH]
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: describer

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    # Two asks: the analysis and the insistence. git did the patch for free.
    assert len(describer.prompts) == 2
    assert github.issues == []
    assert len(github.calls) == 1

    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:main.py"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.stdout == "items = [0]\n"


async def test_an_unappliable_patch_still_files_the_issue(setup):
    """A diff against files that do not exist must not half-land."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    repo_config.mode = "fix"
    workspace = daemon.workspaces["owner/name"]
    edit = workspace.path / "main.py"

    class Describer(FakeAgent):
        async def chat(self, message):
            self.prompts.append(message)
            return CompletionResponse(
                content=self.replies.pop(0), usage=dict(self.usage_per_call)
            )

    describer = Describer()
    describer.replies = [DESCRIBED_BUT_NOT_APPLIED, REPORT_WITH_STALE_PATCH]
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: describer

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert github.calls == []
    assert len(github.issues) == 1
    assert edit.read_text() == "items = []\n"  # nothing touched


async def test_suggest_mode_never_applies_reported_patches(setup):
    """Suggest mode has no branch to land them on."""
    daemon, logfile, agent, github, store, remote = setup
    workspace = daemon.workspaces["owner/name"]

    agent.report = REPORT_WITH_PATCH

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(github.issues) == 1
    assert (workspace.path / "main.py").read_text() == "items = []\n"


# ---------------------------------------------------------------------------
# A failing suite earns one repair round
# ---------------------------------------------------------------------------


async def test_a_failing_suite_earns_one_repair_round(setup, tmp_path):
    """The failing output goes to the model before the PR opens, then the
    suite runs once more."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])
    marker = tmp_path / "repaired-marker"
    # Names the file the agent edits, which is what earns the round.
    repo_config.test_command = (
        f"test -f {marker} || ( echo 'FAILED main.py::test_items'; exit 1 )"
    )

    class Repairing(FakeAgent):
        async def chat(self, message):
            self.prompts.append(message)
            # Edits land on the first ask, so the insist round never fires;
            # the marker appears only after the repair prompt arrives.
            self.edit_path.write_text("items = [0]\n")
            if len(self.prompts) > 1:
                marker.write_text("done")
            return CompletionResponse(content=REPORT, usage=dict(self.usage_per_call))

    repairing = Repairing(edit_path=daemon.workspaces["owner/name"].path / "main.py")
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: repairing
    repo_config.mode = "fix"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    # Exactly one extra round: analysis, then the repair.
    assert len(repairing.prompts) == 2
    assert "Fix your own fix" in repairing.prompts[1]
    # The repair sees what failed, not just that it failed.
    assert f"`{repo_config.test_command}`" in repairing.prompts[1]
    # The second run's verdict is what ships.
    assert "Tests pass" in github.calls[0]["body"]


async def test_a_suite_that_still_fails_after_repair_opens_honestly(setup):
    """One round, not a loop: a repair that did not help ships anyway, with
    the failure in the body."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(
        daemon, agent, test_command="echo 'boom: main.py still failing'; exit 1"
    )

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    # Exactly one extra round: analysis, then the repair — never a loop.
    assert len(agent.prompts) == 2
    assert "Fix your own fix" in agent.prompts[1]
    assert len(github.calls) == 1
    body = github.calls[0]["body"]
    assert "Tests fail" in body
    assert "boom: main.py still failing" in body


async def test_passing_tests_are_never_asked_to_repair(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent, test_command="exit 0")

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(agent.prompts) == 1
    assert not any("Fix your own fix" in p for p in agent.prompts)


async def test_a_failed_repair_round_still_opens_the_pr(setup):
    """A provider blip during the repair must not kill a ready run."""
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])

    class DyingRepairer(FakeAgent):
        async def chat(self, message):
            self.prompts.append(message)
            if len(self.prompts) > 1:
                raise RuntimeError("provider down")
            self.edit_path.write_text("items = [0]\n")
            return CompletionResponse(content=REPORT, usage=dict(self.usage_per_call))

    dying = DyingRepairer(edit_path=daemon.workspaces["owner/name"].path / "main.py")
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: dying
    repo_config.mode = "fix"
    repo_config.test_command = "exit 1"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(github.calls) == 1
    assert "Tests fail" in github.calls[0]["body"]


async def test_a_first_report_with_the_patch_is_never_asked_again(setup):
    """`git apply` is free; the insistence is a whole ask with the tool
    history resent. A first report carrying the patch must not pay for it."""
    daemon, logfile, agent, github, store, remote = setup
    daemon.repo_for(daemon.monitors[0]).mode = "fix"

    class Describer(FakeAgent):
        async def chat(self, message):
            self.prompts.append(message)
            return CompletionResponse(
                content=REPORT_WITH_PATCH, usage=dict(self.usage_per_call)
            )

    describer = Describer()
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: describer

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    # One ask, not two: the patch was in the report the first time.
    assert len(describer.prompts) == 1
    assert not any("You changed no files" in p for p in describer.prompts)
    assert github.issues == []
    assert len(github.calls) == 1
    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:main.py"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert show.stdout == "items = [0]\n"


async def test_a_suite_that_was_already_red_earns_no_repair_round(setup):
    """A repo whose suite is already red would otherwise buy a round on every
    incident, forever, for a failure the fix did not cause."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(
        daemon, agent,
        test_command="echo 'FAILED tests/test_billing.py::test_invoice'; exit 1",
    )

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    # One ask only — the analysis. No repair was worth paying for.
    assert len(agent.prompts) == 1
    assert not any("Fix your own fix" in p for p in agent.prompts)
    # And the PR still opens, saying why the red suite is not the fix's fault.
    body = github.calls[0]["body"]
    assert "Tests fail" in body
    assert "names none of the files this change touches" in body


def test_a_failure_naming_an_edited_file_is_ours():
    assert blames_our_edits(
        "tests/test_cart.py:12: in total\n    cart/totals.py:8: KeyError",
        ["cart/totals.py", "tests/test_cart.py"],
    )


def test_a_failure_naming_nothing_we_touched_is_not_ours():
    assert not blames_our_edits(
        "FAILED tests/test_billing.py::test_invoice - AssertionError",
        ["cart/totals.py"],
    )


def test_a_failure_with_no_edits_at_all_is_not_ours():
    """Nothing changed means nothing to blame."""
    assert not blames_our_edits("FAILED something", [])


# ---------------------------------------------------------------------------
# Fix mode records the change; the rest becomes its own issue
# ---------------------------------------------------------------------------

FIXED_WITH_FOLLOW_UP = """# main.py indexes a list the caller may leave empty

## What happened
Requests to /items with an empty cart returned a 500.

## Root cause
`main.py:1` reads `items[0]` without checking the list.

## Applied fix
`main.py:1` — seeds the list so the index is always valid, and how to verify
it: request /items with an empty cart and read a 200 back.

## Follow-up
### Guard empty order line access
- Evidence: `handlers/orders.py:44` reads `lines[0]` although callers allow an empty list.
- Change: Return the established empty-order response before indexing the collection.
- Acceptance: A regression test passes for an order whose lines collection is empty.
"""

FIXED_AND_COMPLETE = FIXED_WITH_FOLLOW_UP.replace(
    """## Follow-up
### Guard empty order line access
- Evidence: `handlers/orders.py:44` reads `lines[0]` although callers allow an empty list.
- Change: Return the established empty-order response before indexing the collection.
- Acceptance: A regression test passes for an order whose lines collection is empty.
""",
    "## Follow-up\nNone\n",
)


async def test_fix_mode_is_asked_to_record_the_change_not_propose_it(setup):
    """Both modes used to be asked for "## Suggested fix", diff and all."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    prompt = agent.prompts[0]
    assert "## Applied fix" in prompt
    assert "## Follow-up" in prompt
    assert "## Suggested fix" not in prompt
    assert "Do not paste the diff back" in prompt


async def test_suggest_mode_still_proposes_a_diff(setup):
    daemon, logfile, agent, github, store, remote = setup

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    prompt = agent.prompts[0]
    assert "## Suggested fix" in prompt
    assert "## Follow-up" not in prompt


async def test_the_follow_up_is_filed_as_its_own_issue(setup):
    """In the PR body it reads as work the diff does; as an issue it reads as
    what it is."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    agent.report = FIXED_WITH_FOLLOW_UP

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    pr = github.calls[0]
    assert "## Applied fix" in pr["body"]
    # The suggestions are gone from the change...
    assert "## Follow-up" not in pr["body"]
    assert "handlers/orders.py:44" not in pr["body"]
    # ...and filed where they can be acted on, pointing back at the fix.
    assert len(github.issues) == 1
    issue = github.issues[0]
    assert issue["title"].startswith("[maajun] Follow-up: ")
    assert "handlers/orders.py:44" in issue["body"]
    assert "## Acceptance criteria" in issue["body"]
    assert pr_url_of(github) in issue["body"]


async def test_a_repair_response_cannot_put_follow_up_text_back_in_the_pr(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])

    class RepairWithFollowUp(FakeAgent):
        async def chat(self, message):
            self.prompts.append(message)
            self.edit_path.write_text("items = [0]\n")
            content = FIXED_AND_COMPLETE if len(self.prompts) == 1 else FIXED_WITH_FOLLOW_UP
            return CompletionResponse(content=content, usage=dict(self.usage_per_call))

    repairing = RepairWithFollowUp(
        edit_path=daemon.workspaces["owner/name"].path / "main.py"
    )
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: repairing
    repo_config.mode = "fix"
    repo_config.test_command = "echo 'FAILED main.py::test_items'; exit 1"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(repairing.prompts) == 2
    assert "## Follow-up" not in github.calls[0]["body"]
    assert len(github.issues) == 1
    assert "Guard empty order line access" in github.issues[0]["title"]


async def test_each_valid_follow_up_task_gets_its_own_issue(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    second = """
### Add an empty collection factory
- Evidence: `tests/factories.py:12` creates orders but requires at least one line.
- Change: Allow the order factory to build an explicitly empty lines collection.
- Acceptance: Factory tests pass when called with an empty lines collection.
"""
    agent.report = FIXED_WITH_FOLLOW_UP + second

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(github.issues) == 2
    assert "Guard empty order line access" in github.issues[0]["title"]
    assert "Add an empty collection factory" in github.issues[1]["title"]


async def test_invalid_follow_up_is_rewritten_once_without_repeating_valid_tasks(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    invalid = """
### Maybe investigate later
- Evidence: unknown
- Change: Look into it.
"""
    rewritten = """### Add an empty collection factory
- Evidence: `tests/factories.py:12` requires every order to contain a line.
- Change: Allow the order factory to build an explicitly empty lines collection.
- Acceptance: Factory tests pass when called with an empty lines collection.
"""
    agent.replies = [FIXED_WITH_FOLLOW_UP + invalid, rewritten]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(agent.prompts) == 2
    assert "Maybe investigate later" in agent.prompts[1]
    assert "Guard empty order line access" not in agent.prompts[1]
    assert len(github.issues) == 2


async def test_invalid_follow_up_is_skipped_after_one_failed_rewrite(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    invalid = """
## Follow-up
Maybe investigate an unrelated test failure and the SMTP environment.
"""
    agent.replies = [REPORT + invalid, "Still vague and unsupported."]

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(agent.prompts) == 2
    assert github.issues == []
    assert "Follow-up" not in github.calls[0]["body"]


async def test_follow_up_issue_failures_do_not_stop_later_tasks(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    second = """
### Add an empty collection factory
- Evidence: `tests/factories.py:12` requires every order to contain a line.
- Change: Allow the order factory to build an explicitly empty lines collection.
- Acceptance: Factory tests pass when called with an empty lines collection.
"""
    agent.report = FIXED_WITH_FOLLOW_UP + second
    original_create = github.create_issue

    async def fail_first(repo, *, title, body):
        if "Guard empty" in title:
            raise RuntimeError("422")
        return await original_create(repo, title=title, body=body)

    github.create_issue = fail_first

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(github.calls) == 1
    assert len(github.issues) == 1
    assert "Add an empty collection factory" in github.issues[0]["title"]


async def test_follow_up_rewrite_temporarily_disables_edit_permission(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo_config = daemon.repo_for(daemon.monitors[0])

    class PermissionAwareAgent(FakeAgent):
        def __init__(self, edit_path):
            super().__init__(edit_path=edit_path)
            self.approve = "write-policy"

        async def chat(self, message):
            if self.prompts:
                assert self.approve is None
            return await super().chat(message)

    aware = PermissionAwareAgent(daemon.workspaces[repo_config.repo].path / "main.py")
    aware.replies = [
        REPORT + "\n## Follow-up\nMaybe inspect this later.",
        "None",
    ]
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: aware
    repo_config.mode = "fix"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert aware.approve == "write-policy"
    assert len(aware.prompts) == 2


async def test_no_more_than_three_follow_up_issues_are_filed(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    tasks = "\n".join(
        f"""### Add regression guard number {number}
- Evidence: `handlers/orders{number}.py:44` indexes a collection that callers may leave empty.
- Change: Return the established empty response before indexing this collection.
- Acceptance: A regression test passes for handler number {number} with an empty collection.
"""
        for number in range(1, 5)
    )
    agent.report = REPORT + f"\n## Follow-up\n{tasks}"

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(github.issues) == 3


async def test_a_complete_fix_files_no_follow_up(setup):
    """An issue that says "None" is worse than no issue."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    agent.report = FIXED_AND_COMPLETE

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    await daemon.poll_once()

    assert len(github.calls) == 1
    assert github.issues == []


async def test_a_follow_up_that_cannot_be_filed_does_not_fail_the_run(setup):
    """The pull request is already open and it carries the fix."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    agent.report = FIXED_WITH_FOLLOW_UP

    async def refuse(repo, *, title, body):
        raise RuntimeError("422 Unprocessable")

    github.create_issue = refuse

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    handled = await daemon.poll_once()

    assert len(handled) == 1
    assert len(github.calls) == 1


async def test_the_committed_report_leaves_the_follow_up_out(setup):
    """It is part of the diff being reviewed, so it says what the body says."""
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    agent.report = FIXED_WITH_FOLLOW_UP

    with open(logfile, "a") as f:
        f.write(TRACEBACK)
    fp = (await daemon.poll_once())[0]

    show = subprocess.run(
        ["git", "show", f"maajun/incident-{fp}:docs/incidents/{fp}.md"],
        cwd=str(remote), capture_output=True, text=True,
    )
    assert "## Applied fix" in show.stdout
    assert "handlers/orders.py:44" not in show.stdout


def pr_url_of(github) -> str:
    return f"https://github.com/{github.calls[0]['repo']}/pull/1"
