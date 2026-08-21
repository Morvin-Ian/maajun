from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from maajun.agent.core import Agent
from maajun.auth import AuthManager
from maajun.chat.memory import ChatMemory
from maajun.chat.permissions import ALWAYS, chat_permissions
from maajun.chat.prompt import build_system_prompt
from maajun.chat.tools import chat_registry
from maajun.cli.shared import prompt_line
from maajun.config import Config
from maajun.daemon.store import IncidentStore
from maajun.progress import WorkingStatus
from maajun.providers.base import ProviderError
from maajun.providers.factory import ProviderFactory
from maajun.providers.pricing import extract_usage
from maajun.utils import truncate, utc_day_start_iso

log = logging.getLogger(__name__)

EXIT_WORDS = frozenset({"/exit", "/quit", "exit", "quit"})

# A slash command is one bare word. "/var/log/app.log is full of errors" is a
# sentence about a path, and belongs to the model.
SLASH = re.compile(r"^/[a-z]+$")

# How many past messages of a resumed session are replayed into the agent's context.
RESUME_MESSAGES = 20

COMMANDS = (
    "/help", "/commands", "/sessions", "/history", "/cost", "/clear", "/new",
    "/resume", "/model", "/provider", "/forget", "/exit",
)

HELP = """\
[bold]Slash commands[/bold]

  [cyan]/help[/cyan]      this message
  [cyan]/commands[/cyan]  every maajun command, with a one-line summary
  [cyan]/sessions[/cyan]  recent chat sessions and their ids
  [cyan]/history[/cyan]   this session so far
  [cyan]/cost[/cyan]      what this session has cost
  [cyan]/clear[/cyan]     forget this session's context (the record is kept)
  [cyan]/new[/cyan]       start a fresh session
  [cyan]/resume[/cyan]    [dim]<id>[/dim] carry an earlier session on
  [cyan]/model[/cyan]     [dim][name][/dim] show or switch the model
  [cyan]/provider[/cyan]  [dim][name][/dim] show or switch the AI provider
  [cyan]/forget[/cyan]    [dim]<id|all>[/dim] delete a stored conversation
  [cyan]/exit[/cyan]      leave

Anything else is a message. Ask about maajun, ask it to change a setting,
or ask what it found last week.
"""


class TurnView:
    """A spinner while the model is thinking, plain text once it answers.

    The spinner is a Live region and the answer is ordinary output, so the
    two are never on screen at once: anything printed stops the animation
    first, and a question waiting for input is never drawn over.
    """

    def __init__(self, console: Console):
        self.console = console
        self.live: Live | None = None
        self.status: WorkingStatus | None = None
        self.opened = False

    def waiting(self, phase: str = "Thinking") -> None:
        # The status is held here, not read back off the Live: Live.renderable
        # hands back whatever it wrapped ours in, which is not ours to call.
        if self.live is None:
            self.status = WorkingStatus(phase)
            self.live = Live(
                self.status,
                console=self.console,
                refresh_per_second=8,
                transient=True,
            )
            self.live.start()
        else:
            self.status.set(phase)

    def quiet(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
            self.status = None

    def text(self, chunk: str) -> None:
        self.quiet()
        if not self.opened:
            self.console.print()
            self.opened = True
        self.console.out(chunk, end="", highlight=False)

    def tool(self, line: str) -> None:
        self.quiet()
        if self.opened:
            self.console.print()
        self.console.print(f"[dim]{' '.join(line.split())}[/dim]")
        self.opened = False

    def close(self) -> None:
        self.quiet()
        if self.opened:
            self.console.print()


class ChatSession:
    def __init__(
        self,
        config: Config,
        *,
        console: Console,
        store: IncidentStore,
        memory: ChatMemory,
        session_id: int,
        ask: object = None,
        interactive: bool = True,
        history_path: Path | None = None,
    ):
        self.config = config
        self.console = console
        self.store = store
        self.memory = memory
        self.session_id = session_id
        self.interactive = interactive
        self.ask = ask
        self.history_path = history_path
        self.reader = None
        self.runner = asyncio.Runner()
        self.view: TurnView | None = None
        self.agent = self.build_agent()


    @contextlib.contextmanager
    def quiet(self) -> Iterator[None]:
        """Stop drawing while a tool captures the process's stdout.

        The spinner is a Live region on this thread and Rich writes to
        whatever sys.stdout is at the time, so leaving it running while
        run_cli holds the redirect paints the animation into the captured
        output. stream_reply starts it again on the next chunk.
        """
        view = self.view
        if view is not None:
            view.quiet()
        yield

    def build_agent(self) -> Agent:
        return Agent(
            self.config,
            tools=chat_registry(
                self.config, self.store, self.memory, self.session_id, self.quiet
            ),
            approve=chat_permissions(self.ask_permission),
            system_prompt=build_system_prompt(),
        )

    def replace_agent(self) -> None:
        """Rebuild the agent around new settings, carrying the context over."""
        previous = self.agent
        history = previous.history
        try:
            self.runner.run(previous.aclose())
        except Exception:
            log.debug("closing the previous agent failed", exc_info=True)
        self.agent = self.build_agent()
        self.agent.history = history


    def read(self) -> str:
        if self.ask is not None:
            return self.ask("> ")
        session = self.prompt_session()
        if session is None:
            return prompt_line("\n> ")
        self.console.print()
        return session.prompt("> ")

    def prompt_session(self):
        """A prompt_toolkit session with recall and slash completion.

        Built once, and only for a real terminal: the history file and the
        completer are meaningless when input is a pipe.
        """
        if self.reader is not None:
            return self.reader if self.reader is not False else None
        if not sys.stdin.isatty():
            self.reader = False
            return None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import FileHistory

            history = None
            if self.history_path is not None:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(self.history_path))
            self.reader = PromptSession(
                history=history,
                completer=WordCompleter(list(COMMANDS)),
                complete_while_typing=False,
            )
        except Exception:
            log.debug("falling back to the plain prompt", exc_info=True)
            self.reader = False
        return self.reader if self.reader is not False else None

    def confirm(self, description: str) -> bool:
        """Show what is about to happen and wait for a plain yes or no."""
        view = self.view
        if view is not None:
            view.quiet()
        if not self.interactive:
            self.console.print(
                f"\n[yellow]▸ Not run (no one to ask):[/yellow] {description}"
            )
            return False
        self.console.print(f"\n[yellow]▸ Run:[/yellow] [bold]{description}[/bold]")
        answer = self.read_confirmation()
        approved = answer.strip().lower() in ("y", "yes")
        if not approved:
            self.console.print("[dim]Skipped.[/dim]")
        return approved

    def ask_permission(self, description: str) -> bool | str:
        """Approve one tool call, remember the answer, or say why not.

        Anything that is not a yes, a no, or 'always' is taken as an
        instruction and handed to the model, so declining can redirect the
        work instead of just stopping it.
        """
        view = self.view
        if view is not None:
            view.quiet()
        if not self.interactive:
            self.console.print(
                f"\n[yellow]▸ Not run (no one to ask):[/yellow] {description}"
            )
            return False

        self.console.print(f"\n[yellow]▸ Run:[/yellow] [bold]{description}[/bold]")
        self.console.print(
            "[dim]  y = yes · a = always, for this tool · n = no · "
            "anything else = what to do instead[/dim]"
        )
        answer = self.read_confirmation().strip()
        lowered = answer.lower()
        if lowered in ("y", "yes"):
            return True
        if lowered in ("a", ALWAYS):
            self.console.print("[dim]Won't ask again this session.[/dim]")
            return ALWAYS
        self.console.print("[dim]Skipped.[/dim]")
        return False if lowered in ("", "n", "no") else answer

    def read_confirmation(self) -> str:
        if self.ask is not None:
            return self.ask("  Run it? (y/N): ")

        return prompt_line("  Run it? (y/N): ")


    def loop(self) -> None:
        """Read, answer, repeat. The caller greets and resumes first."""
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
            if SLASH.match(message.split()[0].lower()):
                self.handle_slash(message)
                continue
            try:
                self.turn(message)
            except KeyboardInterrupt:
                self.console.print("\n[dim]Interrupted.[/dim]")

    def turn(self, message: str) -> None:
        """One user message and the agent's reply, recorded either way."""
        if self.over_budget():
            return
        self.memory.add_message(self.session_id, "user", message)
        self.view = TurnView(self.console)
        try:
            answer = self.runner.run(self.stream_reply(message))
        except ProviderError as e:
            # Already a user-facing message; the turn is not recorded as an
            # answer because there wasn't one.
            self.console.print(f"[red]✗ {e}[/red]")
        except Exception as e:
            log.debug("chat turn failed", exc_info=True)
            self.console.print(f"[red]✗ {type(e).__name__}: {e}[/red]")
        else:
            if answer:
                self.memory.add_message(self.session_id, "assistant", answer)
        finally:
            self.view.close()
            self.view = None
            self.record_usage()

    async def stream_reply(self, message: str) -> str:
        """Stream one reply, printing it as it arrives. Returns the text.

        Reasoning is not printed. A model that thinks out loud is thinking
        for itself, not talking to the user, and the spinner already says
        it is working.
        """
        view = self.view
        parts: list[str] = []
        view.waiting()
        async for kind, data in self.agent.chat_stream(message):
            if kind == "content":
                parts.append(data)
                view.text(data)
            elif kind == "running":
                view.waiting(f"Running {data}")
            elif kind == "tool":
                view.tool(data)
                view.waiting()
        return "".join(parts).strip()

    def record_usage(self) -> None:
        prompt_tokens, completion_tokens, cost = extract_usage(
            self.agent.take_usage(), self.agent.model
        )
        if prompt_tokens or completion_tokens:
            self.memory.record_usage(
                self.session_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
            )

    def over_budget(self) -> bool:
        cap = self.config.chat.max_usd_per_day
        if not cap:
            return False
        spent = self.memory.cost_since(utc_day_start_iso())
        if spent < cap:
            return False
        self.console.print(
            f"[yellow]⚠ Chat has spent ${spent:.2f} today, at the "
            f"${cap:.2f} cap.[/yellow]\n"
            "[dim]Raise it with 'maajun config chat.max_usd_per_day 20' "
            "(0 disables it).[/dim]"
        )
        return True


    def handle_slash(self, message: str) -> None:
        command, _, argument = message.partition(" ")
        command, argument = command.lower(), argument.strip()
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
        elif command == "/new":
            self.start_new()
        elif command == "/resume":
            self.resume(argument)
        elif command == "/model":
            self.switch_model(argument)
        elif command == "/provider":
            self.switch_provider(argument)
        elif command == "/forget":
            self.forget(argument)
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
            "\n[dim]Carry one on with '/resume <id>'.[/dim]"
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
        cap = self.config.chat.max_usd_per_day
        limit = (
            f"[dim]Today's chat cap: ${cap:.2f} "
            f"(spent ${self.memory.cost_since(utc_day_start_iso()):.4f}).[/dim]"
            if cap
            else "[dim]No chat cap set (chat.max_usd_per_day = 0).[/dim]"
        )
        self.console.print(
            f"\nThis session: [bold]${row.get('cost_usd', 0):.4f}[/bold] "
            f"[dim]({row.get('prompt_tokens', 0):,} prompt + "
            f"{row.get('completion_tokens', 0):,} completion tokens)[/dim]\n"
            f"All chats:    [bold]${self.memory.total_cost():.4f}[/bold]\n"
            f"{limit}"
        )

    def start_new(self) -> None:
        self.session_id = self.memory.start_session()
        self.replace_agent()
        self.agent.clear_history()
        self.console.print(f"[dim]Started session {self.session_id}.[/dim]")

    def resume(self, argument: str) -> None:
        if not argument.isdigit():
            self.console.print(
                "[yellow]Which session?[/yellow] [dim]/resume <id> — "
                "see /sessions.[/dim]"
            )
            return
        session_id = int(argument)
        if self.memory.session(session_id) is None:
            self.console.print(f"[yellow]No chat session {session_id}.[/yellow]")
            return
        self.session_id = session_id
        self.replace_agent()
        self.agent.clear_history()
        self.resume_from(session_id)

    def switch_model(self, name: str) -> None:
        if not name:
            self.console.print(f"Model: [cyan]{self.agent.model}[/cyan]")
            return
        self.config.ai.model = name
        self.replace_agent()
        self.console.print(f"[dim]Now using {self.agent.model}.[/dim]")

    def switch_provider(self, name: str) -> None:
        if not name:
            self.console.print(f"Provider: [cyan]{self.config.ai.provider}[/cyan]")
            return
        supported = [p.value for p in ProviderFactory.get_supported_providers()]
        if name not in supported:
            self.console.print(
                f"[yellow]Unknown provider {name!r}.[/yellow] "
                f"[dim]Choose one of: {', '.join(supported)}.[/dim]"
            )
            return
        api_key = AuthManager().get_api_key(name)
        if not api_key:
            self.console.print(
                f"[yellow]No API key stored for {name}.[/yellow] "
                "[dim]Run 'maajun setup' to add one.[/dim]"
            )
            return
        previous = self.config.ai.provider
        self.config.ai.provider = name
        self.config.ai.api_key = api_key
        # The model was chosen for the provider being left behind; clearing it
        # falls back to the new provider's default.
        self.config.ai.model = None
        self.replace_agent()
        self.console.print(
            f"[dim]Switched from {previous} to {name} ({self.agent.model}).[/dim]"
        )

    def forget(self, argument: str) -> None:
        if argument == "all":
            if not self.confirm("delete every stored chat conversation"):
                return
            count = self.memory.delete_all()
            self.session_id = self.memory.start_session()
            self.replace_agent()
            self.agent.clear_history()
            self.console.print(f"[dim]Deleted {count} conversations.[/dim]")
            return
        if not argument.isdigit():
            self.console.print(
                "[yellow]Which session?[/yellow] [dim]/forget <id>, or "
                "/forget all.[/dim]"
            )
            return
        session_id = int(argument)
        if session_id == self.session_id:
            self.console.print(
                "[yellow]That is this conversation.[/yellow] "
                "[dim]Start another with /new first.[/dim]"
            )
            return
        if self.memory.delete_session(session_id):
            self.console.print(f"[dim]Deleted session {session_id}.[/dim]")
        else:
            self.console.print(f"[yellow]No chat session {session_id}.[/yellow]")


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
            self.runner.run(self.agent.aclose())
        except Exception:
            log.debug("closing the agent failed", exc_info=True)
        self.runner.close()
        self.memory.close()
        self.store.close()


def run_chat_session(
    config: Config,
    *,
    console: Console,
    workdir: Path,
    resume: int | None = None,
    prompt: str = "",
) -> None:
    """Open the stores, build a session, and run it to completion."""
    database = workdir / "incidents.db"
    store = IncidentStore(database)
    memory = ChatMemory(database)

    if resume is not None and memory.session(resume) is None:
        memory.close()
        store.close()
        raise ValueError(f"No chat session {resume}.")

    # A resumed session is carried on, not copied: its cost, its title and its
    # transcript stay in one place instead of fragmenting across rows.
    session_id = resume if resume is not None else memory.start_session()
    chat = ChatSession(
        config,
        console=console,
        store=store,
        memory=memory,
        session_id=session_id,
        interactive=not prompt,
        history_path=workdir / "chat_history",
    )
    try:
        if prompt:
            chat.turn(prompt)
            return
        chat.greet()
        if resume is not None:
            chat.resume_from(resume)
        chat.loop()
    finally:
        chat.close()
