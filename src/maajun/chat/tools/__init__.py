from pathlib import Path

from maajun.agent.tools import BUILTIN_TOOLS, Sandbox, ToolRegistry
from maajun.chat.memory import ChatMemory
from maajun.chat.tools.commands import QuietScope, command_tools, unquieted
from maajun.chat.tools.incidents import incident_tools
from maajun.chat.tools.recall import recall_tools
from maajun.config import Config
from maajun.daemon.store import IncidentStore


def chat_sandbox(config: Config) -> Sandbox:
    """Where chat may read and write: this project, and maajun's own files.

    The working directory is the project the user opened chat in, and
    daemon.workdir holds the clones. Configured log files are named one by
    one rather than by their directory — /var/log is not the project.
    """
    log_files = list(config.monitor.log_files)
    for repo_config in config.github.get_all_repos():
        log_files.extend(repo_config.log_files)
    return Sandbox([
        Path.cwd(),
        Path(config.daemon.workdir).expanduser(),
        *log_files,
    ])


def chat_registry(
    config: Config,
    store: IncidentStore,
    memory: ChatMemory,
    session_id: int,
    quiet: QuietScope = unquieted,
) -> ToolRegistry:
    """Build the registry for one chat session.

    The recall tools are bound to `session_id` so they search everything
    except the conversation already in the agent's context. `quiet` lets the
    session take its spinner down while a command tool captures stdout.
    """
    return ToolRegistry(
        [
            *BUILTIN_TOOLS,
            *command_tools(quiet),
            *incident_tools(store),
            *recall_tools(memory, session_id),
        ],
        chat_sandbox(config),
    )


__all__ = [
    "chat_registry",
    "chat_sandbox",
    "command_tools",
    "incident_tools",
    "recall_tools",
]
