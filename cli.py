"""CLI for mycode.

This is intentionally small. The canonical implementation is the backend agent loop.
The CLI reuses the same agent + session store.

Usage:

  uv run python cli.py

Environment:
  MODEL     e.g. anthropic:claude-sonnet-4-5
  BASE_URL  optional (OpenAI-compatible base URL)
"""

from __future__ import annotations

import asyncio
import hashlib
import os

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.rule import Rule
from rich.text import Text

from app.agent.core import Agent
from app.config import get_settings
from app.session import SessionStore

console = Console(highlight=False)

# History file lives next to the script so it persists across sessions
_HISTORY_FILE = os.path.join(os.path.dirname(__file__), ".cli_history")


def _tool_preview(args: dict) -> str:
    """Return a short preview string from the first tool argument."""
    if not args:
        return ""
    value = str(next(iter(args.values())))
    return value[:60] + "…" if len(value) > 60 else value


async def chat_loop(agent: Agent, *, store: SessionStore, session_id: str) -> None:
    session: PromptSession = PromptSession(history=FileHistory(_HISTORY_FILE))

    async def on_persist(message: dict) -> None:
        await store.append_message(session_id, message)

    while True:
        # Separator before prompt
        console.print(Rule(style="dim"))

        try:
            user_input: str = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.prompt("❯ "),
            )
        except KeyboardInterrupt:
            # Ctrl-C at prompt — cancel any in-flight request (there isn't one here)
            continue
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            return

        user_input = user_input.strip()
        if not user_input:
            continue

        # --- Built-in commands ---
        if user_input in ("/q", "exit", "quit"):
            console.print("[dim]Goodbye![/dim]")
            return

        if user_input in ("/c", "/clear"):
            await store.clear_session(session_id)
            agent.clear()
            console.print("[green]✓[/green] Conversation cleared")
            continue

        if user_input in ("/h", "/help"):
            console.print(
                "[dim]/c[/dim]  clear conversation\n[dim]/q[/dim]  quit\n[dim]Ctrl-C[/dim]  cancel current request"
            )
            continue

        # --- Stream response ---
        console.print(Rule(style="dim"))

        text_buffer: list[str] = []
        live: Live | None = None

        try:
            async for event in agent.achat(user_input, on_persist=on_persist):
                if event.type == "text":
                    chunk = event.data.get("content", "")
                    text_buffer.append(chunk)
                    full = "".join(text_buffer)
                    if live is None:
                        live = Live(Markdown(full), console=console, refresh_per_second=12)
                        live.start()
                    else:
                        live.update(Markdown(full))

                elif event.type == "tool_start":
                    if live is not None:
                        live.stop()
                        live = None
                    text_buffer.clear()
                    name = event.data.get("name", "")
                    args = event.data.get("args") or {}
                    preview = _tool_preview(args)
                    label = Text()
                    label.append("▸ ", style="dim")
                    label.append(name, style="cyan bold")
                    if preview:
                        label.append(f"  {preview}", style="dim")
                    console.print(label)

                elif event.type == "tool_output":
                    line = event.data.get("content", "")
                    if line:
                        console.print(f"  [dim]{line}[/dim]")

                elif event.type == "tool_done":
                    result = event.data.get("result", "")
                    first = (result.splitlines() or [""])[0][:80]
                    if result.startswith("ok"):
                        console.print(f"  [green]✓[/green] [dim]{first}[/dim]")
                    elif result.startswith("error"):
                        console.print(f"  [red]✗[/red] {first}")
                    else:
                        console.print(f"  [dim]↳ {first}[/dim]")

                elif event.type == "error":
                    if live is not None:
                        live.stop()
                        live = None
                    text_buffer.clear()
                    msg = event.data.get("message", "")
                    console.print(f"\n[red bold]Error:[/red bold] {msg}")

        except KeyboardInterrupt:
            agent.cancel()
            if live is not None:
                live.stop()
                live = None
            text_buffer.clear()
            console.print("\n[dim]Cancelled[/dim]")
            continue

        # Stop live if response ends with text
        if live is not None:
            live.stop()
            live = None
        text_buffer.clear()


def main() -> None:
    settings = get_settings()
    model = os.environ.get("MODEL") or settings.default_model or "anthropic:claude-sonnet-4-5"
    api_base = os.environ.get("BASE_URL") or settings.api_base

    cwd = os.getcwd()
    store = SessionStore()

    # One default session per working directory (pi-style)
    session_id = hashlib.sha1(cwd.encode()).hexdigest()[:12]

    data = asyncio.run(store.get_or_create(session_id, model=model, cwd=cwd, api_base=api_base))
    messages = data.get("messages") or []

    agent = Agent(
        model=model,
        cwd=cwd,
        session_dir=store.session_dir(session_id),
        api_base=api_base,
        messages=messages,
    )

    # Header
    console.print()
    header = Text()
    header.append("mycode", style="bold")
    header.append("  ")
    header.append(model, style="cyan")
    header.append("  ")
    header.append(cwd, style="dim")
    console.print(header)
    console.print("[dim]/h help  /c clear  /q quit  Ctrl-C cancel[/dim]")

    asyncio.run(chat_loop(agent, store=store, session_id=session_id))


if __name__ == "__main__":
    main()
