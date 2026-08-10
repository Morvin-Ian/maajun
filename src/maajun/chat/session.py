"""The `maajun chat` read-eval-print loop."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from maajun.agent.core import Agent
from maajun.chat.memory import ChatMemory
from maajun.chat.permissions import chat_permissions
from maajun.chat.prompt import build_system_prompt
from maajun.chat.tools import chat_registry
from maajun.config import Config
from maajun.daemon.store import IncidentStore
from maajun.progress import working
from maajun.providers.base import ProviderError
from maajun.providers.pricing import extract_usage
from maajun.utils import truncate

log = logging.getLogger(__name__)

EXIT_WORDS = frozenset({"/exit", "/quit", "exit", "quit"})

# How many past messages of a resumed session are replayed into the agent's
# context. The rest stays searchable through recall_session rather than being
# paid for on every request.
RESUME_MESSAGES = 20

HELP = """\
[bold]Slash commands[/bold]

  [cyan]/help[/cyan]      this message
  [cyan]/commands[/cyan]  every maajun command, with a one-line summary
  [cyan]/sessions[/cyan]  recent chat sessions and their ids
  [cyan]/history[/cyan]   this session so far
  [cyan]/cost[/cyan]      what this session has cost
  [cyan]/clear[/cyan]     forget this session's context (the record is kept)
  [cyan]/exit[/cyan]      leave

Anything else is a message. Ask about maajun, ask it to change a setting,
or ask what it found last week.
"""


class ChatSession:
    """One conversation: an agent, its memory, and the loop that drives them."""

    def __init__(
        self,
        config: Config,
        *,
        console: Console,
        store: IncidentStore,
        memory: ChatMemory,
        session_id: int,
        ask: object = None,
    ):
        self.config = config
        self.console = console
        self.store = store
        self.memory = memory
        self.session_id = session_id
        self._ask = ask
        self.agent = Agent(
            config,
            tools=chat_registry(store, memory, session_id),
            approve=chat_permissions(self.confirm),
            system_prompt=build_system_prompt(),
        )

    # -- input/output -----------------------------------------------------

    def read(self) -> str:
        """One line from the user. Imported lazily so tests can drive it."""
        if self._ask is not None:
            return self._ask("> ")
        from maajun.cli._shared import prompt_line

        return prompt_line("\n> ")

    def confirm(self, description: str) -> bool:
        """Show what is about to run and wait for a yes.

        Called from inside the agent's tool loop, so it prints above whatever
        the turn has produced so far rather than through the spinner.
        """
        self.console.print(f"\n[yellow]▸ Run:[/yellow] [bold]{description}[/bold]")
        answer = self.read_confirmation()
        approved = answer.strip().lower() in ("y", "yes")
        if not approved:
            self.console.print("[dim]Skipped.[/dim]")
        return approved

    def read_confirmation(self) -> str:
        if self._ask is not None:
            return self._ask("  Run it? (y/N): ")
        from maajun.cli._shared import prompt_line

        return prompt_line("  Run it? (y/N): ")

    # -- the loop ---------------------------------------------------------

    def run(self) -> None:
        self.greet()
        self.loop()

    def loop(self) -> None:
        while True:
            try:
                message = self.read().strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]Bye.[/dim]")
                return
            if not message:
                continue
            if message.lower() in EXIT_WORDS:
                self.console.print("[dim]Bye.[/dim]")
                return
            if message.startswith("/"):
                self.handle_slash(message)
                continue
            try:
                self.turn(message)
            except KeyboardInterrupt:
                self.console.print("\n[dim]Interrupted.[/dim]")

    def turn(self, message: str) -> None:
        """One user message and the agent's reply, recorded either way."""
        self.memory.add_message(self.session_id, "user", message)
        try:
            with working(self.console, "Thinking"):
                response = asyncio.run(self.agent.chat(message))
        except ProviderError as e:
            # Already a user-facing message; the turn is not recorded as an
            # answer because there wasn't one.
            self.console.print(f"[red]✗ {e}[/red]")
            return
        except Exception as e:
            log.debug("chat turn failed", exc_info=True)
            self.console.print(f"[red]✗ {type(e).__name__}: {e}[/red]")
            return

        answer = (response.content or "").strip()
        if answer:
            self.console.print()
            self.console.print(Markdown(answer))
            self.memory.add_message(self.session_id, "assistant", answer)

        prompt_tokens, completion_tokens, cost = extract_usage(
            response.usage, getattr(response, "model", None)
        )
        self.memory.record_usage(
            self.session_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )

    # -- slash commands ---------------------------------------------------

    def handle_slash(self, message: str) -> None:
        command = message.split()[0].lower()
        if command == "/help":
            self.console.print(HELP)
        elif command == "/commands":
            self.show_commands()
        elif command == "/sessions":
            self.show_sessions()
        elif command == "/history":
            self.show_history()
        elif command == "/cost":
            self.show_cost()
        elif command == "/clear":
            self.agent.clear_history()
            self.console.print(
                "[dim]Context cleared. The conversation is still on record "
                "and searchable.[/dim]"
            )
        else:
            self.console.print(
                f"[yellow]Unknown command {command}.[/yellow] "
                "[dim]Try /help.[/dim]"
            )

    def show_commands(self) -> None:
        from maajun.chat.tools.commands import Gate, command_index

        self.console.print("\n[bold]maajun commands[/bold]\n")
        for info in command_index():
            note = (
                " [dim](not from chat)[/dim]" if info.gate is Gate.BLOCKED else ""
            )
            self.console.print(f"  [cyan]{info.name:<16}[/cyan]{info.help}{note}")

    def show_sessions(self) -> None:
        sessions = self.memory.recent_sessions()
        self.console.print("\n[bold]Recent sessions[/bold]\n")
        for row in sessions:
            marker = " [green]← this one[/green]" if row["id"] == self.session_id else ""
            title = row["title"] or "(untitled)"
            self.console.print(
                f"  [cyan]{row['id']:>4}[/cyan]  {truncate(title, 48, '…'):<50}"
                f"[dim]{row['message_count']} msgs  {row['updated_at']}[/dim]{marker}"
            )
        self.console.print(
            "\n[dim]Resume one with 'maajun chat --session <id>'.[/dim]"
        )

    def show_history(self) -> None:
        messages = self.memory.messages(self.session_id)
        if not messages:
            self.console.print("[dim]Nothing yet.[/dim]")
            return
        for entry in messages:
            who = "[cyan]you[/cyan]" if entry["role"] == "user" else "[green]maajun[/green]"
            self.console.print(f"\n{who}: {truncate(entry['content'], 500, '…')}")

    def show_cost(self) -> None:
        row = self.memory.session(self.session_id) or {}
        self.console.print(
            f"\nThis session: [bold]${row.get('cost_usd', 0):.4f}[/bold] "
            f"[dim]({row.get('prompt_tokens', 0):,} prompt + "
            f"{row.get('completion_tokens', 0):,} completion tokens)[/dim]\n"
            f"All chats:    [bold]${self.memory.total_cost():.4f}[/bold]\n"
            "[dim]Chat spend is recorded but not capped — "
            "daemon.max_usd_per_day applies to the daemon only.[/dim]"
        )

    # -- lifecycle --------------------------------------------------------

    def greet(self) -> None:
        repos = [rc.repo for rc in self.config.github.get_all_repos()]
        where = ", ".join(repos) if repos else "local mode (no repo configured)"
        self.console.print(Panel(
            "[bold]Maajun chat[/bold]\n\n"
            f"Provider: [cyan]{self.config.ai.provider}[/cyan]\n"
            f"Repos:    [cyan]{where}[/cyan]\n\n"
            "Ask about maajun, ask it to change a setting, or ask what it "
            "found before.\n"
            "[dim]/help for commands · /exit to leave[/dim]",
            border_style="blue",
        ))

    def resume_from(self, session_id: int) -> None:
        """Replay a past session's tail into the agent's context."""
        messages = self.memory.messages(session_id, limit=RESUME_MESSAGES)
        for entry in messages:
            if entry["role"] in ("user", "assistant"):
                self.agent.history.append(
                    {"role": entry["role"], "content": entry["content"]}
                )
        self.console.print(
            f"[dim]Resumed session {session_id} "
            f"({len(messages)} messages of context).[/dim]"
        )

    def close(self) -> None:
        try:
            asyncio.run(self.agent.aclose())
        except Exception:
            log.debug("closing the agent failed", exc_info=True)
        self.memory.close()
        self.store.close()


def run_chat_session(
    config: Config,
    *,
    console: Console,
    workdir: Path,
    resume: int | None = None,
) -> None:
    """Open the stores, build a session, and run it to completion."""
    database = workdir / "incidents.db"
    store = IncidentStore(database)
    memory = ChatMemory(database)

    if resume is not None and memory.session(resume) is None:
        memory.close()
        store.close()
        raise ValueError(f"No chat session {resume}.")

    session_id = memory.start_session()
    chat = ChatSession(
        config, console=console, store=store, memory=memory, session_id=session_id
    )
    try:
        chat.greet()
        if resume is not None:
            chat.resume_from(resume)
        chat.loop()
    finally:
        chat.close()
