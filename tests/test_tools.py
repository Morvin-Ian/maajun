"""Tests for agent tools (read_file, edit_file, glob, grep, bash, etc.)."""

import os

import pytest

from maajun.agent.tools import (
    BASH,
    EDIT_FILE,
    GIT_STATUS,
    GLOB,
    GREP,
    LIST_DIR,
    READ_FILE,
    WRITE_FILE,
    default_registry,
)

# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_source(tmp_path):
    """Create a temporary source file."""
    f = tmp_path / "hello.py"
    f.write_text("line 1\nline 2\nline 3\n")
    return f


async def test_read_file_returns_numbered_lines(tmp_source):
    _, executor = READ_FILE
    result = await executor(path=str(tmp_source))
    assert "1: line 1" in result
    assert "2: line 2" in result
    assert "3: line 3" in result


async def test_read_file_with_offset_and_limit(tmp_source):
    _, executor = READ_FILE
    result = await executor(path=str(tmp_source), offset=1, limit=1)
    assert "2: line 2" in result
    assert "line 1" not in result
    assert "line 3" not in result


async def test_read_file_missing_file():
    _, executor = READ_FILE
    result = await executor(path="/nonexistent/file.txt")
    assert "Error" in result


async def test_read_file_directory(tmp_path):
    _, executor = READ_FILE
    result = await executor(path=str(tmp_path))
    assert "Error" in result
    assert "directory" in result


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


async def test_edit_file_replaces_string(tmp_source):
    _, executor = EDIT_FILE
    result = await executor(
        path=str(tmp_source),
        old_string="line 2",
        new_string="LINE 2",
    )
    assert "Edited" in result
    assert tmp_source.read_text() == "line 1\nLINE 2\nline 3\n"


async def test_edit_file_not_found(tmp_source):
    _, executor = EDIT_FILE
    result = await executor(
        path=str(tmp_source),
        old_string="nonexistent",
        new_string="x",
    )
    assert "not found" in result


async def test_edit_file_multiple_matches(tmp_source):
    tmp_source.write_text("foo\nfoo\nfoo\n")
    _, executor = EDIT_FILE
    result = await executor(
        path=str(tmp_source),
        old_string="foo",
        new_string="bar",
    )
    assert "found 3 times" in result


async def test_edit_file_missing_file():
    _, executor = EDIT_FILE
    result = await executor(
        path="/nonexistent/file.txt",
        old_string="x",
        new_string="y",
    )
    assert "Error" in result


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


async def test_write_file_creates_file(tmp_path):
    _, executor = WRITE_FILE
    target = tmp_path / "new.py"
    result = await executor(path=str(target), content="hello\n")
    assert "Wrote" in result
    assert target.read_text() == "hello\n"


async def test_write_file_creates_parent_dirs(tmp_path):
    _, executor = WRITE_FILE
    target = tmp_path / "a" / "b" / "c.txt"
    await executor(path=str(target), content="nested")
    assert target.read_text() == "nested"


async def test_write_file_overwrites(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old")
    _, executor = WRITE_FILE
    await executor(path=str(f), content="new")
    assert f.read_text() == "new"


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


async def test_glob_finds_files(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    _, executor = GLOB
    result = await executor(pattern="*.py", path=str(tmp_path))
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


async def test_glob_no_matches(tmp_path):
    _, executor = GLOB
    result = await executor(pattern="*.xyz", path=str(tmp_path))
    assert "No files matched" in result


async def test_glob_missing_path():
    _, executor = GLOB
    result = await executor(pattern="*", path="/nonexistent")
    assert "Error" in result


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


async def test_grep_finds_matches(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n")
    _, executor = GREP
    result = await executor(pattern="def foo", path=str(tmp_path))
    assert "a.py:1:" in result
    assert "def foo" in result


async def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello\n")
    _, executor = GREP
    result = await executor(pattern="nonexistent", path=str(tmp_path))
    assert "No matches" in result


async def test_grep_with_include_filter(tmp_path):
    (tmp_path / "a.py").write_text("target\n")
    (tmp_path / "b.txt").write_text("target\n")
    _, executor = GREP
    result = await executor(
        pattern="target", path=str(tmp_path), include="*.py"
    )
    assert "a.py" in result
    assert "b.txt" not in result


async def test_grep_invalid_regex(tmp_path):
    _, executor = GREP
    result = await executor(pattern="[invalid", path=str(tmp_path))
    assert "invalid regex" in result


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


async def test_bash_echo():
    _, executor = BASH
    result = await executor(command="echo hello")
    assert "hello" in result


async def test_bash_returns_stderr():
    _, executor = BASH
    result = await executor(command="python3 -c 'import sys; sys.stderr.write(\"err\\n\")'")
    assert "err" in result


async def test_bash_exit_code():
    _, executor = BASH
    result = await executor(command="exit 42")
    assert "exit code: 42" in result


async def test_bash_timeout(tmp_path):
    _, executor = BASH
    result = await executor(command="sleep 10", timeout=1)
    assert "timed out" in result


async def test_bash_empty_command():
    _, executor = BASH
    result = await executor(command="")
    assert "empty command" in result


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


async def test_list_dir(tmp_path):
    (tmp_path / "file.txt").write_text("")
    (tmp_path / "subdir").mkdir()
    _, executor = LIST_DIR
    result = await executor(path=str(tmp_path))
    assert "file.txt" in result
    assert "subdir/" in result


async def test_list_dir_empty(tmp_path):
    _, executor = LIST_DIR
    result = await executor(path=str(tmp_path))
    assert "empty" in result


async def test_list_dir_missing():
    _, executor = LIST_DIR
    result = await executor(path="/nonexistent")
    assert "Error" in result


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


async def test_git_status(tmp_path):
    _, executor = GIT_STATUS
    result = await executor(path=str(tmp_path))
    assert "Not a git repository" in result or "Error" in result


async def test_git_status_in_repo(tmp_path):
    os.system(f"cd {tmp_path} && git init -q")
    _, executor = GIT_STATUS
    result = await executor(path=str(tmp_path))
    assert "Branch:" in result


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


def test_default_registry_has_all_tools():
    reg = default_registry()
    names = {d.name for d in reg.definitions()}
    expected = {
        "read_file", "edit_file", "write_file", "glob",
        "grep", "bash", "list_dir", "git_status",
    }
    assert names == expected


async def test_registry_execute_known_tool():
    reg = default_registry()
    result = await reg.execute("bash", {"command": "echo registry_works"})
    assert "registry_works" in result


async def test_registry_execute_unknown_tool():
    reg = default_registry()
    result = await reg.execute("nonexistent_tool", {})
    assert "unknown tool" in result
