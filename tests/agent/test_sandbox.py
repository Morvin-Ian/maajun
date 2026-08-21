"""Tests for the boundary on what the tools may read and write.

Everything a tool opens is sent to the AI provider, and in the daemon's case
can be quoted into a public issue — so these are the tests that decide what
leaves the machine.
"""

import pytest

from maajun.agent.tools import BUILTIN_TOOLS, Sandbox, ToolRegistry, default_registry


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / ".env").write_text("API_KEY=sk-real-secret\n")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "private.txt").write_text("not yours\n")
    return root


@pytest.fixture
def registry(project):
    return ToolRegistry(BUILTIN_TOOLS, Sandbox([project]))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_a_file_in_the_project_is_readable(registry, project):
    result = await registry.execute("read_file", {"path": str(project / "src/app.py")})
    assert "print('hi')" in result


async def test_a_file_outside_the_project_is_refused(registry, tmp_path):
    result = await registry.execute(
        "read_file", {"path": str(tmp_path / "elsewhere" / "private.txt")}
    )
    assert "outside the directories maajun may touch" in result
    assert "not yours" not in result


async def test_the_refusal_names_what_is_allowed_instead(registry, project):
    result = await registry.execute("read_file", {"path": "/etc/passwd"})
    assert str(project) in result


async def test_a_dotenv_inside_the_project_is_refused(registry, project):
    """The allowed root is not a licence to read the credentials in it."""
    result = await registry.execute("read_file", {"path": str(project / ".env")})
    assert "holds credentials" in result
    assert "sk-real-secret" not in result


@pytest.mark.parametrize(
    "name",
    [".env", ".env.production", "id_rsa", "server.pem", "server.key", ".netrc",
     ".git-credentials"],
)
async def test_credential_files_are_refused_by_name(registry, project, name):
    (project / name).write_text("secret")
    result = await registry.execute("read_file", {"path": str(project / name)})
    assert "holds credentials" in result


async def test_the_incident_database_is_not_readable(registry, project):
    """It holds every incident and every conversation anyone has had."""
    (project / "incidents.db").write_bytes(b"SQLite format 3\x00")
    result = await registry.execute(
        "read_file", {"path": str(project / "incidents.db")}
    )
    assert "maajun's own database" in result
    assert "search_incidents" in result


async def test_git_internals_are_not_readable(registry, project):
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[remote]\n\turl = https://token@x/y\n")
    result = await registry.execute(
        "read_file", {"path": str(project / ".git" / "config")}
    )
    assert ".git directory" in result
    assert "token" not in result


async def test_a_symlink_out_of_the_project_is_refused(registry, project, tmp_path):
    """The check runs on the resolved path, or a link is a way straight out."""
    (project / "escape.txt").symlink_to(tmp_path / "elsewhere" / "private.txt")
    result = await registry.execute("read_file", {"path": str(project / "escape.txt")})
    assert "outside the directories" in result
    assert "not yours" not in result


async def test_a_relative_path_that_climbs_out_is_refused(registry, project):
    result = await registry.execute(
        "read_file", {"path": str(project / ".." / "elsewhere" / "private.txt")}
    )
    assert "outside the directories" in result


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def test_a_write_outside_the_project_is_refused(registry, tmp_path):
    target = tmp_path / "elsewhere" / "planted.py"
    result = await registry.execute(
        "write_file", {"path": str(target), "content": "malware"}
    )
    assert "outside the directories" in result
    assert not target.exists()


async def test_an_edit_outside_the_project_is_refused(registry, tmp_path):
    target = tmp_path / "elsewhere" / "private.txt"
    result = await registry.execute(
        "edit_file",
        {"path": str(target), "old_string": "not yours", "new_string": "mine"},
    )
    assert "outside the directories" in result
    assert target.read_text() == "not yours\n"


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


async def test_searching_outside_the_project_is_refused(registry, tmp_path):
    result = await registry.execute(
        "grep", {"pattern": "not yours", "path": str(tmp_path / "elsewhere")}
    )
    assert "outside the directories" in result
    assert "private.txt" not in result


async def test_listing_outside_the_project_is_refused(registry, tmp_path):
    result = await registry.execute("list_dir", {"path": str(tmp_path / "elsewhere")})
    assert "outside the directories" in result
    assert "private.txt" not in result


async def test_grep_does_not_return_a_dotenv_it_walked_into(registry, project):
    """The registry gates the path grep is handed; grep then reads the tree
    under it. Without a per-file gate the .env comes back as matched lines."""
    result = await registry.execute(
        "grep", {"pattern": "sk-real", "path": str(project)}
    )
    assert "sk-real-secret" not in result
    assert ".env" not in result
    assert "1 off-limits files skipped" in result


@pytest.mark.parametrize(
    "name", ["id_rsa", "server.pem", ".env.production", ".git-credentials"]
)
async def test_grep_skips_credential_files_by_name(registry, project, name):
    # The pattern and the secret differ: grep echoes the pattern back in its
    # header, so asserting on the pattern would fail on the header alone.
    (project / name).write_text("token=hunter2-do-not-leak\n")
    result = await registry.execute(
        "grep", {"pattern": "token", "path": str(project)}
    )
    assert "hunter2-do-not-leak" not in result
    assert name not in result


async def test_grep_will_not_follow_a_symlink_out_of_the_project(
    registry, project, tmp_path
):
    """Judged on the resolved path: the link is inside, the target is not."""
    (project / "notes.txt").symlink_to(tmp_path / "elsewhere" / "private.txt")
    result = await registry.execute(
        "grep", {"pattern": "yours", "path": str(project)}
    )
    assert "not yours" not in result
    assert "notes.txt" not in result


async def test_grep_still_searches_ordinary_files(registry, project):
    result = await registry.execute("grep", {"pattern": "hi", "path": str(project)})
    assert "src/app.py" in result


async def test_grep_is_unrestricted_without_a_sandbox(project):
    """The no-sandbox registry is for tests and library use; it gates nothing."""
    plain = ToolRegistry(BUILTIN_TOOLS)
    result = await plain.execute("grep", {"pattern": "API_KEY", "path": str(project)})
    assert "sk-real-secret" in result


async def test_a_glob_cannot_climb_out_of_its_root(registry, project):
    """The root is checked before the call; '..' would escape it afterwards."""
    result = await registry.execute(
        "glob", {"pattern": "../elsewhere/*.txt", "path": str(project)}
    )
    assert "not allowed in a glob pattern" in result
    assert "private.txt" not in result


async def test_glob_still_works_within_the_root(registry, project):
    result = await registry.execute("glob", {"pattern": "src/*.py", "path": str(project)})
    assert "app.py" in result


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_a_registry_without_a_sandbox_is_unrestricted():
    """The default is for tests and library use; both real ones pass a sandbox."""
    assert default_registry().sandbox is None


def test_the_chat_registry_is_sandboxed(tmp_path):
    from maajun.chat.memory import ChatMemory
    from maajun.chat.tools import chat_registry
    from maajun.config import Config, DaemonConfig
    from maajun.daemon.store import IncidentStore

    database = tmp_path / "incidents.db"
    store, memory = IncidentStore(database), ChatMemory(database)
    config = Config(daemon=DaemonConfig(workdir=str(tmp_path / "data")))

    registry = chat_registry(config, store, memory, memory.start_session())
    assert registry.sandbox is not None
    assert registry.sandbox.contains(tmp_path.resolve() / "data" / "workspaces")
    assert not registry.sandbox.contains(tmp_path.resolve().parent / "elsewhere")
    store.close()
    memory.close()


def test_configured_log_files_are_reachable_but_not_their_directory(tmp_path):
    from maajun.chat.tools import chat_sandbox
    from maajun.config import Config, DaemonConfig, MonitorConfig

    log = tmp_path / "logs" / "app.log"
    log.parent.mkdir()
    log.write_text("ERROR boom\n")
    sandbox = chat_sandbox(Config(
        monitor=MonitorConfig(log_files=[str(log)]),
        daemon=DaemonConfig(workdir=str(tmp_path / "data")),
    ))

    assert sandbox.contains(log.resolve())
    assert not sandbox.contains(log.parent.resolve() / "other.log")


async def test_omitting_the_path_does_not_skip_the_check(project, tmp_path, monkeypatch):
    """grep and list_dir fall back to the working directory when path is
    omitted — which is a way out whenever the sandbox is not the cwd."""
    monkeypatch.chdir(tmp_path / "elsewhere")
    registry = ToolRegistry(BUILTIN_TOOLS, Sandbox([project]))

    assert "outside the directories" in await registry.execute("list_dir", {})
    assert "outside the directories" in await registry.execute(
        "grep", {"pattern": "not yours"}
    )


async def test_omitting_the_path_is_fine_when_the_cwd_is_allowed(project, monkeypatch):
    monkeypatch.chdir(project)
    registry = ToolRegistry(BUILTIN_TOOLS, Sandbox([project]))

    assert "app.py" in await registry.execute("glob", {"pattern": "src/*.py"})


async def test_tools_without_a_path_are_untouched(project):
    """A sandbox is about the filesystem; it must not gate the other tools."""
    from maajun.agent.tools.base import Tool, json_schema
    from maajun.providers.base import ToolDefinition

    async def run(path: str = "") -> str:
        return f"ran with {path!r}"

    tool = Tool(
        ToolDefinition(name="not_a_file_tool", description="", parameters=json_schema({})),
        run,
    )
    registry = ToolRegistry([tool], Sandbox([project]))

    assert await registry.execute("not_a_file_tool", {"path": "/etc/passwd"}) == (
        "ran with '/etc/passwd'"
    )
