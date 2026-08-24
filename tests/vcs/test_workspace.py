import subprocess

import pytest

from maajun.vcs import GitError, GitWorkspace


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


# ---------------------------------------------------------------------------
# Applying the patch a report carried
# ---------------------------------------------------------------------------

MAIN_PY_PATCH = (
    "--- a/main.py\n"
    "+++ b/main.py\n"
    "@@ -1 +1 @@\n"
    "-items = []\n"
    "+items = [0]\n"
)


def seeded_workspace(tmp_path) -> GitWorkspace:
    """A one-commit clone standing in for a synced workspace."""
    workspace = GitWorkspace(tmp_path / "ws", "owner/name")
    workspace.path.mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
        cwd=str(workspace.path), check=True, capture_output=True,
    )
    run("init", "-b", "main")
    (workspace.path / "main.py").write_text("items = []\n")
    run("add", "-A")
    run("commit", "-m", "initial")
    return workspace


async def test_apply_patches_lands_a_clean_diff(tmp_path):
    workspace = seeded_workspace(tmp_path)

    await workspace.apply_patches([MAIN_PY_PATCH])

    assert (workspace.path / "main.py").read_text() == "items = [0]\n"
    assert await workspace.has_changes()


async def test_one_stale_patch_leaves_the_whole_tree_untouched(tmp_path):
    """Half a described fix is worse than none of it."""
    workspace = seeded_workspace(tmp_path)
    stale = MAIN_PY_PATCH.replace("main.py", "services/cart.py")

    with pytest.raises(GitError):
        await workspace.apply_patches([MAIN_PY_PATCH, stale])

    assert (workspace.path / "main.py").read_text() == "items = []\n"
    assert not await workspace.has_changes()


async def test_a_patch_that_no_longer_fits_is_rejected(tmp_path):
    workspace = seeded_workspace(tmp_path)

    stale_context = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old = 1\n+new = 2\n"
    with pytest.raises(GitError):
        await workspace.apply_patches([stale_context])

    assert (workspace.path / "main.py").read_text() == "items = []\n"


async def test_two_patches_for_one_file_land_together_or_not_at_all(tmp_path):
    """A fix and its regression test arrive as two fences against the same
    file. Both fit the pristine tree, so checking them one at a time passes,
    and the second then fails on top of the first."""
    workspace = seeded_workspace(tmp_path)
    (workspace.path / "main.py").write_text("a = 1\nb = 2\nc = 3\n")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-am", "grow"],
        cwd=str(workspace.path), check=True, capture_output=True,
    )
    first = "--- a/main.py\n+++ b/main.py\n@@ -1,3 +1,3 @@\n-a = 1\n+a = 99\n b = 2\n c = 3\n"
    overlapping = (
        "--- a/main.py\n+++ b/main.py\n@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 77\n c = 3\n"
    )

    with pytest.raises(GitError):
        await workspace.apply_patches([first, overlapping])

    assert (workspace.path / "main.py").read_text() == "a = 1\nb = 2\nc = 3\n"
    assert not await workspace.has_changes()


async def test_apply_patches_does_nothing_with_no_patches(tmp_path):
    workspace = seeded_workspace(tmp_path)

    await workspace.apply_patches([])

    assert not await workspace.has_changes()
