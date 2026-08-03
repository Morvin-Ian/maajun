"""Tests for GitWorkspace — the isolated clone the daemon works in."""

import subprocess

from maajun.vcs import GitWorkspace


async def test_run_command_captures_output_and_exit_code(tmp_path):
    workspace = GitWorkspace(tmp_path, "owner/name")
    workspace.path.mkdir(parents=True, exist_ok=True)

    ok = await workspace.run_command("echo hello")
    assert ok.passed and "hello" in ok.output

    bad = await workspace.run_command("echo to-stderr >&2; exit 3")
    assert not bad.passed
    assert bad.exit_code == 3
    assert "to-stderr" in bad.output


async def test_run_command_times_out_without_raising(tmp_path):
    workspace = GitWorkspace(tmp_path, "owner/name")
    workspace.path.mkdir(parents=True, exist_ok=True)

    result = await workspace.run_command("sleep 5", timeout=0.2)
    assert result.exit_code is None
    assert not result.passed
    assert "Timed out" in result.output


async def test_run_command_runs_in_the_workspace(tmp_path):
    workspace = GitWorkspace(tmp_path, "owner/name")
    workspace.path.mkdir(parents=True, exist_ok=True)
    (workspace.path / "marker.txt").write_text("x")

    result = await workspace.run_command("ls")
    assert "marker.txt" in result.output


async def test_recent_commits_returns_sha_and_subject(tmp_path):
    workspace = GitWorkspace(tmp_path, "owner/name")
    workspace.path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
        cwd=str(workspace.path), check=True, capture_output=True,
    )
    run("init", "-b", "main")
    (workspace.path / "a.txt").write_text("1")
    run("add", "-A")
    run("commit", "-m", "Add the cart totals")

    commits = await workspace.recent_commits()
    assert len(commits) == 1
    assert "Add the cart totals" in commits[0]


async def test_recent_commits_is_empty_without_history(tmp_path):
    workspace = GitWorkspace(tmp_path, "owner/name")
    workspace.path.mkdir(parents=True, exist_ok=True)
    assert await workspace.recent_commits() == []
