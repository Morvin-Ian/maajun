"""The chat agent's tool set: the builtins, plus maajun's own memory and CLI."""

from maajun.agent.tools import BUILTIN_TOOLS, ToolRegistry
from maajun.chat.memory import ChatMemory
from maajun.chat.tools.commands import command_tools
from maajun.chat.tools.incidents import incident_tools
from maajun.chat.tools.recall import recall_tools
from maajun.daemon.store import IncidentStore


def chat_registry(
    store: IncidentStore, memory: ChatMemory, session_id: int
) -> ToolRegistry:
    """Build the registry for one chat session.

    The recall tools are bound to `session_id` so they search everything
    except the conversation already in the agent's context.
    """
    return ToolRegistry([
        *BUILTIN_TOOLS,
        *command_tools(),
        *incident_tools(store),
        *recall_tools(memory, session_id),
    ])


__all__ = ["chat_registry", "command_tools", "incident_tools", "recall_tools"]
