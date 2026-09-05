import subprocess

from daemon.fakes import REPORT, TRACEBACK, FakeAgent, fix_mode
from maajun.config import DeploymentConfig
from maajun.daemon.store import ARTIFACT_ISSUE, ARTIFACT_REPORT


async def test_public_runtime_issue_stays_local_by_default(setup):
    daemon, logfile, agent, github, store, remote = setup
    github.visibilities["owner/name"] = "public"

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    fingerprint = (await daemon.poll_once())[0]

    assert github.issues == []
    row = store.get(fingerprint, "owner/name")
    assert row["artifact_kind"] == ARTIFACT_REPORT
    report = (daemon.report_dir / f"{fingerprint}.md").read_text()
    assert "Runtime publication policy" in report
    assert "is public" in report
    assert "Draft repository change" not in report


async def test_public_runtime_issue_uses_configured_private_repo(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo = daemon.repo_for(daemon.monitors[0])
    repo.runtime_artifact_repo = "owner/private-incidents"
    github.visibilities.update({
        "owner/name": "public",
        "owner/private-incidents": "private",
    })

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    fingerprint = (await daemon.poll_once())[0]

    assert github.issues[0]["repo"] == "owner/private-incidents"
    assert "routed to the configured non-public" in github.issues[0]["body"]
    assert store.get(fingerprint, "owner/name")["artifact_kind"] == ARTIFACT_ISSUE


async def test_public_runtime_issue_can_be_explicitly_enabled(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo = daemon.repo_for(daemon.monitors[0])
    repo.allow_public_runtime_artifacts = True
    github.visibilities[repo.repo] = "public"

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    await daemon.poll_once()

    assert github.issues[0]["repo"] == "owner/name"


async def test_public_fix_is_not_pushed_and_becomes_a_private_issue(setup):
    daemon, logfile, agent, github, store, remote = setup
    fix_mode(daemon, agent)
    repo = daemon.repo_for(daemon.monitors[0])
    repo.runtime_artifact_repo = "owner/private-incidents"
    github.visibilities.update({
        "owner/name": "public",
        "owner/private-incidents": "private",
    })

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    await daemon.poll_once()

    branches = subprocess.run(
        ["git", "branch", "-a"],
        cwd=str(remote),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "maajun/incident-" not in branches
    assert github.calls == []
    assert github.issues[0]["repo"] == "owner/private-incidents"
    assert "Draft repository change (not published)" in github.issues[0]["body"]
    assert "No code change" not in github.issues[0]["body"]


async def test_public_infrastructure_target_keeps_withheld_fix_local(setup):
    daemon, logfile, author, github, store, remote = setup
    fix_mode(daemon, author)
    repo = daemon.repo_for(daemon.monitors[0])
    repo.deployment = DeploymentConfig(
        service_command="/srv/app/.venv/bin/uvicorn app:api",
        proxy_config_path="/etc/nginx/sites-available/api.example.com",
        config_owner="operator",
        infra_repo="owner/infrastructure",
    )
    author.edit_path = daemon.workspaces[repo.repo].path / "nginx.conf"
    first_critic = FakeAgent("PASS")
    final_critic = FakeAgent(
        "BLOCK\nIssue title: Raise the active nginx request-body limit\n"
        "The operator-owned proxy still rejects the request."
    )
    agents = iter((author, first_critic, final_critic))
    daemon.agent_factory_for_repo = lambda rc, ws: lambda: next(agents)
    github.visibilities.update({
        repo.repo: "private",
        "owner/infrastructure": "public",
    })

    with open(logfile, "a") as stream:
        stream.write(TRACEBACK)
    fingerprint = (await daemon.poll_once())[0]

    assert github.calls == []
    assert github.issues == []
    assert github.visibility_calls == [repo.repo, "owner/infrastructure"]
    row = store.get(fingerprint, repo.repo)
    assert row["artifact_kind"] == ARTIFACT_REPORT
    report = (daemon.report_dir / f"{fingerprint}.md").read_text()
    assert "owner/infrastructure is public" in report
    assert "Infrastructure routing" in report


async def test_owner_initiated_manual_report_is_not_visibility_gated(setup):
    daemon, logfile, agent, github, store, remote = setup
    repo = daemon.repo_for(daemon.monitors[0])
    github.visibilities[repo.repo] = "public"

    await daemon.handle_manual_report("Checkout fails for an empty cart", repo)

    assert github.issues[0]["repo"] == repo.repo
    assert github.visibility_calls == []
    assert REPORT in github.issues[0]["body"]
