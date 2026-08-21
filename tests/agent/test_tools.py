"""Tests for agent tools (read_file, edit_file, glob, grep, etc.)."""

import os

import pytest

from maajun.agent.tools import (
    EDIT_FILE,
    GIT_STATUS,
    GLOB,
    GREP,
    LIST_DIR,
    MAX_TOOL_RESULT_CHARS,
    READ_FILE,
    WRITE_FILE,
    cap_result,
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
    executor = READ_FILE.executor
    result = await executor(path=str(tmp_source))
    assert "1: line 1" in result
    assert "2: line 2" in result
    assert "3: line 3" in result


async def test_read_file_with_offset_and_limit(tmp_source):
    executor = READ_FILE.executor
    result = await executor(path=str(tmp_source), offset=1, limit=1)
    assert "2: line 2" in result
    assert "line 1" not in result
    assert "line 3" not in result


async def test_read_file_missing_file():
    executor = READ_FILE.executor
    result = await executor(path="/nonexistent/file.txt")
    assert "Error" in result


async def test_read_file_directory(tmp_path):
    executor = READ_FILE.executor
    result = await executor(path=str(tmp_path))
    assert "Error" in result
    assert "directory" in result


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


async def test_edit_file_replaces_string(tmp_source):
    executor = EDIT_FILE.executor
    result = await executor(
        path=str(tmp_source),
        old_string="line 2",
        new_string="LINE 2",
    )
    assert "Edited" in result
    assert tmp_source.read_text() == "line 1\nLINE 2\nline 3\n"


async def test_edit_file_not_found(tmp_source):
    executor = EDIT_FILE.executor
    result = await executor(
        path=str(tmp_source),
        old_string="nonexistent",
        new_string="x",
    )
    assert "not found" in result


async def test_edit_file_multiple_matches(tmp_source):
    tmp_source.write_text("foo\nfoo\nfoo\n")
    executor = EDIT_FILE.executor
    result = await executor(
        path=str(tmp_source),
        old_string="foo",
        new_string="bar",
    )
    assert "found 3 times" in result


async def test_edit_file_missing_file():
    executor = EDIT_FILE.executor
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
    executor = WRITE_FILE.executor
    target = tmp_path / "new.py"
    result = await executor(path=str(target), content="hello\n")
    assert "Wrote" in result
    assert target.read_text() == "hello\n"


async def test_write_file_creates_parent_dirs(tmp_path):
    executor = WRITE_FILE.executor
    target = tmp_path / "a" / "b" / "c.txt"
    await executor(path=str(target), content="nested")
    assert target.read_text() == "nested"


async def test_write_file_overwrites(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old")
    executor = WRITE_FILE.executor
    await executor(path=str(f), content="new")
    assert f.read_text() == "new"


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


async def test_glob_finds_files(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    executor = GLOB.executor
    result = await executor(pattern="*.py", path=str(tmp_path))
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


async def test_glob_no_matches(tmp_path):
    executor = GLOB.executor
    result = await executor(pattern="*.xyz", path=str(tmp_path))
    assert "No files matched" in result


async def test_glob_missing_path():
    executor = GLOB.executor
    result = await executor(pattern="*", path="/nonexistent")
    assert "Error" in result


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


async def test_grep_finds_matches(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n")
    executor = GREP.executor
    result = await executor(pattern="def foo", path=str(tmp_path))
    assert "a.py:1:" in result
    assert "def foo" in result


async def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello\n")
    executor = GREP.executor
    result = await executor(pattern="nonexistent", path=str(tmp_path))
    assert "No matches" in result


async def test_grep_with_include_filter(tmp_path):
    (tmp_path / "a.py").write_text("target\n")
    (tmp_path / "b.txt").write_text("target\n")
    executor = GREP.executor
    result = await executor(
        pattern="target", path=str(tmp_path), include="*.py"
    )
    assert "a.py" in result
    assert "b.txt" not in result


async def test_grep_invalid_regex(tmp_path):
    executor = GREP.executor
    result = await executor(pattern="[invalid", path=str(tmp_path))
    assert "invalid regex" in result


async def test_grep_skips_binary_files(tmp_path):
    (tmp_path / "code.py").write_text("target here\n")
    (tmp_path / "blob.bin").write_bytes(b"target\x00\x00more target\n")
    executor = GREP.executor
    result = await executor(pattern="target", path=str(tmp_path))
    assert "code.py" in result
    assert "blob.bin" not in result
    assert "1 files searched" in result  # only the text file was scanned


async def test_grep_skips_oversized_files(tmp_path, monkeypatch):
    from maajun.agent.tools import search

    monkeypatch.setattr(search, "MAX_FILE_SIZE", 16)
    (tmp_path / "small.py").write_text("target\n")
    (tmp_path / "big.py").write_text("target " * 100 + "\n")
    result = await search.grep(pattern="target", path=str(tmp_path))
    assert "small.py" in result
    assert "big.py" not in result


async def test_grep_skips_vendored_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("target\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("target\n")
    result = await GREP.executor(pattern="target", path=str(tmp_path))
    assert "a.py" in result
    assert "node_modules" not in result


async def test_glob_skips_vendored_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.py").write_text("")
    result = await GLOB.executor(pattern="**/*.py", path=str(tmp_path))
    assert "keep.py" in result
    assert "node_modules" not in result


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


async def test_list_dir(tmp_path):
    (tmp_path / "file.txt").write_text("")
    (tmp_path / "subdir").mkdir()
    executor = LIST_DIR.executor
    result = await executor(path=str(tmp_path))
    assert "file.txt" in result
    assert "subdir/" in result


async def test_list_dir_empty(tmp_path):
    executor = LIST_DIR.executor
    result = await executor(path=str(tmp_path))
    assert "empty" in result


async def test_list_dir_missing():
    executor = LIST_DIR.executor
    result = await executor(path="/nonexistent")
    assert "Error" in result


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


async def test_git_status(tmp_path):
    executor = GIT_STATUS.executor
    result = await executor(path=str(tmp_path))
    assert "Not a git repository" in result or "Error" in result


async def test_git_status_in_repo(tmp_path):
    os.system(f"cd {tmp_path} && git init -q")
    executor = GIT_STATUS.executor
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
        "grep", "list_dir", "git_status",
    }
    assert names == expected


async def test_registry_execute_known_tool(tmp_path):
    (tmp_path / "hello.txt").write_text("registry_works")
    reg = default_registry()
    result = await reg.execute("read_file", {"path": str(tmp_path / "hello.txt")})
    assert "registry_works" in result


async def test_registry_execute_unknown_tool():
    reg = default_registry()
    result = await reg.execute("nonexistent_tool", {})
    assert "unknown tool" in result


def test_dangerous_tools_require_permission():
    reg = default_registry()
    for name in ("edit_file", "write_file"):
        assert reg.requires_permission(name)
    for name in ("read_file", "glob", "grep", "list_dir", "git_status"):
        assert not reg.requires_permission(name)
    assert not reg.requires_permission("unknown_tool")


# ---------------------------------------------------------------------------
# tool result size cap
# ---------------------------------------------------------------------------


def test_cap_result_leaves_small_results_alone():
    assert cap_result("short") == "short"


def test_cap_result_truncates_and_reports_the_shortfall():
    capped = cap_result("x" * (MAX_TOOL_RESULT_CHARS + 500))
    assert len(capped) < MAX_TOOL_RESULT_CHARS + 500
    assert capped.startswith("x" * 100)
    assert "truncated: 500 more characters" in capped


async def test_registry_caps_an_oversized_tool_result(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("y" * (MAX_TOOL_RESULT_CHARS * 2))
    reg = default_registry()
    result = await reg.execute("read_file", {"path": str(big)})
    assert "truncated" in result
    assert len(result) < MAX_TOOL_RESULT_CHARS + 500
