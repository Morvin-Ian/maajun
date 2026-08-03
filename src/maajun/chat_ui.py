"""Interactive chat session UI — the streaming REPL behind `maajun chat`.

Kept out of cli.py because it's a small terminal UI (a Live-updating stream,
tool-approval prompts, slash commands) rather than command wiring.
"""

from __future__ import annotations

import asyncio
import json
import sys

from prompt_toolkit import PromptSession
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import CodeBlock, Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from maajun.agent.core import Agent
from maajun.config import Config
from maajun.progress import ThinkingStatus
from maajun.providers.base import ProviderError
from maajun.utils import truncate

STREAM_TAIL_LINES = 10


class _UnpaddedCodeBlock(CodeBlock):
    """A fenced code block you can copy out of the terminal intact.

    Rich's default pads code blocks by one column, which prefixes every line
    with a space — copy a shell command out of it and you paste something that
    won't run. Wrapping is kept (it re-flows long lines but loses no
    characters); only the injected padding goes away.
    """

    def __rich_console__(self, console, options):
        yield Syntax(
            str(self.text).rstrip(),
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            padding=0,
        )


def _rendered(content: str) -> Markdown:
    """Render markdown prose, keeping code blocks copy-safe."""
    markdown = Markdown(content)
    markdown.elements = {
        **markdown.elements,
        "fence": _UnpaddedCodeBlock,
        "code_block": _UnpaddedCodeBlock,
    }
    return markdown


def _format_tool_args(args: dict) -> str:
    return truncate(json.dumps(args, indent=2), 500, "\n... (truncated)")


async def _prompt(session: PromptSession | None, text: str) -> str:
    """Read a line of input, via prompt_toolkit when stdin is a TTY."""
    if session is not None:
        return await session.prompt_async(text)
    return Console().input(text)


def _tail(text: str, limit: int = STREAM_TAIL_LINES) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-limit:])


def _stream_renderable(status: ThinkingStatus, content: str) -> Group:
    """Bounded view of the in-flight response.

    The live region must stay shorter than the terminal — Rich's Live cannot
    rewrite lines that have scrolled off screen, so an unbounded renderable
    leaves a stale copy behind on every refresh. Only the tail of the content
    is shown while streaming; the full response is printed once at the end.
    """
    parts = []
    if content.strip():
        parts.append(Text(_tail(content)))
    parts.append(status)
    return Group(*parts)


def run_chat(agent_config: Config, provider: str, *, auto_approve: bool, console: Console) -> None:
    """Build the agent and run the interactive chat loop until the user quits."""
    # Holds the active Live display so the approval prompt can pause it.
    live_holder: dict = {"live": None}

    # prompt_toolkit reads keys in raw mode: a paste (Ctrl+Shift+V) arrives as
    # one bracketed-paste event and is inserted literally — a multi-line paste
    # stays one message instead of submitting line by line — and unbound key
    # combinations are ignored instead of leaking escape codes into the input.
    prompt_session = PromptSession() if sys.stdin.isatty() else None

    async def approve_always(name: str, args: dict) -> bool:
        return True

    async def approve_interactively(name: str, args: dict) -> bool:
        live = live_holder["live"]
        if live:
            live.stop()
        console.print()
        console.print(Panel(
            f"[bold]{name}[/bold]\n{_format_tool_args(args)}",
            title="[yellow]Tool needs permission[/yellow]",
            border_style="yellow",
        ))
        try:
            answer = (await _prompt(prompt_session, "> Allow this call? (y/N): ")).strip().lower()
        except EOFError:
            answer = ""
        if live:
            live.start()
        return answer == "y"

    agent = Agent(
        agent_config,
        approve=approve_always if auto_approve else approve_interactively,
    )

    permission_note = (
        "[dim]Tools run automatically (--auto-approve).[/dim]"
        if auto_approve
        else "[dim]You'll be asked before commands run or files change.[/dim]"
    )
    console.print(Panel(
        f"[bold]Maajun[/bold]  [dim]({provider})[/dim]\n\n"
        "[dim]Type your message at the > prompt.[/dim]\n"
        f"{permission_note}\n"
        "[dim]/clear  /history  /quit[/dim]",
        border_style="blue",
    ))

    try:
        asyncio.run(_chat_loop(agent, console, live_holder, prompt_session))
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")


async def _chat_loop(agent, console, live_holder=None, prompt_session=None):
    live_holder = live_holder if live_holder is not None else {"live": None}
    while True:
        console.print()
        try:
            user_input = (await _prompt(prompt_session, "> ")).strip()
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            console.print("[dim]Goodbye![/dim]")
            break

        if user_input == "/clear":
            agent.clear_history()
            console.print("[dim]Session cleared.[/dim]")
            continue

        if user_input == "/history":
            if not agent.history:
                console.print("[dim]No messages yet.[/dim]")
            else:
                for message in agent.history:
                    if message["role"] == "user":
                        console.print(f"\n> {message['content']}")
                    elif message["role"] == "assistant" and message.get("content"):
                        console.print()
                        console.print(_rendered(message["content"]))
            continue

        console.print()
        content = ""
        error = None
        status = ThinkingStatus()
        try:
            # transient=True: the bounded streaming view is erased on exit and
            # replaced by a single full print of the response below.
            with Live(console=console, refresh_per_second=8, transient=True) as live:
                live_holder["live"] = live
                live.update(_stream_renderable(status, content))
                try:
                    async for kind, text in agent.chat_stream(user_input):
                        if kind == "tool":
                            # Printed while Live is active, so it lands above
                            # the live region and stays there permanently.
                            console.print(Text(text, style="dim"))
                        elif kind == "content":
                            content += text
                            live.update(_stream_renderable(status, content))
                        # "thinking" chunks are deliberately not shown — the
                        # animated status stands in for the reasoning text.
                finally:
                    live_holder["live"] = None
        except ProviderError as e:
            error = str(e)
        except Exception as e:
            error = f"Unexpected error: {e}"

        if content:
            console.print(_rendered(content))
        if error:
            console.print(f"[red]{error}[/red]")
