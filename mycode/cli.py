"""CLI for mycode.

Usage:
  mycode [--provider NAME] [--model MODEL] [--once MESSAGE]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from mycode.core.agent import Agent
from mycode.core.config import get_settings, resolve_provider
from mycode.core.session import SessionStore

console = Console(highlight=False)

_HISTORY_FILE = os.path.join(os.path.dirname(__file__), ".cli_history")
_PROMPT = ANSI("\033[1m\033[34m❯\033[0m ")


def _sep() -> None:
    width = min(shutil.get_terminal_size().columns, 88)
    console.print(f"[dim]{'─' * width}[/dim]")


def _tool_preview(args: dict) -> str:
    if not args:
        return ""
    value = str(next(iter(args.values())))
    return value[:60] + "…" if len(value) > 60 else value


def _result_preview(result: str) -> str:
    lines = result.splitlines()
    if not lines:
        return ""
    first = lines[0][:72]
    if len(lines) > 1:
        first += f"  [dim]+{len(lines) - 1} lines[/dim]"
    elif len(lines[0]) > 72:
        first += "…"
    return first


async def run_once(agent: Agent, *, store: SessionStore, session_id: str, message: str) -> int:
    async def on_persist(payload: dict) -> None:
        await store.append_message(session_id, payload)

    exit_code = 0

    async for event in agent.achat(message, on_persist=on_persist):
        if event.type == "text":
            chunk = event.data.get("content", "")
            if chunk:
                console.print(chunk, end="", markup=False, highlight=False)
        elif event.type == "tool_start":
            name = event.data.get("name", "")
            args = event.data.get("args") or {}
            preview = _tool_preview(args)
            t = Text()
            t.append("\n⏺ ", style="green")
            t.append(name.capitalize(), style="green")
            if preview:
                t.append(f"({preview})", style="dim")
            console.print(t)
        elif event.type == "tool_output":
            line = event.data.get("content", "")
            if line:
                console.print(f"  [dim]{line}[/dim]")
        elif event.type == "tool_done":
            result = event.data.get("result", "")
            preview = _result_preview(result)
            if result.startswith("error"):
                exit_code = 1
                console.print(f"  [red]⎿  {preview}[/red]")
            else:
                console.print(f"  [dim]⎿  {preview}[/dim]")
        elif event.type == "error":
            exit_code = 1
            console.print(f"\n[red]⏺ {event.data.get('message', '')}[/red]")

    console.print()
    return exit_code


async def chat_loop(agent: Agent, *, store: SessionStore, session_id: str) -> None:
    session: PromptSession = PromptSession(history=FileHistory(_HISTORY_FILE))

    async def on_persist(message: dict) -> None:
        await store.append_message(session_id, message)

    while True:
        _sep()

        try:
            user_input: str = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.prompt(_PROMPT),
            )
        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print("\n[dim]bye[/dim]")
            return

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input in ("/q", "exit", "quit"):
            console.print("[dim]bye[/dim]")
            return

        if user_input in ("/c", "/clear"):
            await store.clear_session(session_id)
            agent.clear()
            console.print("[green]⏺[/green] [dim]cleared[/dim]")
            continue

        if user_input.startswith("/model "):
            new_model = user_input[len("/model ") :].strip()
            if not new_model:
                console.print("[dim]usage: /model <name>[/dim]")
                continue
            agent.model = new_model
            console.print(f"[green]⏺[/green] [dim]model →[/dim] {new_model}")
            continue

        if user_input.startswith("/"):
            console.print("[dim]unknown: /c  /q  /model <name>[/dim]")
            continue

        _sep()

        text_buffer: list[str] = []
        live: Live | None = None

        try:
            async for event in agent.achat(user_input, on_persist=on_persist):
                if event.type == "text":
                    chunk = event.data.get("content", "")
                    text_buffer.append(chunk)
                    full = "".join(text_buffer)
                    if live is None:
                        live = Live(
                            Markdown(full),
                            console=console,
                            refresh_per_second=12,
                        )
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
                    t = Text()
                    t.append("⏺ ", style="green")
                    t.append(name.capitalize(), style="green")
                    if preview:
                        t.append(f"({preview})", style="dim")
                    console.print(t)

                elif event.type == "tool_output":
                    line = event.data.get("content", "")
                    if line:
                        console.print(f"  [dim]{line}[/dim]")

                elif event.type == "tool_done":
                    result = event.data.get("result", "")
                    preview = _result_preview(result)
                    if result.startswith("error"):
                        console.print(f"  [red]⎿  {preview}[/red]")
                    else:
                        console.print(f"  [dim]⎿  {preview}[/dim]")

                elif event.type == "error":
                    if live is not None:
                        live.stop()
                        live = None
                    text_buffer.clear()
                    console.print(f"\n[red]⏺ {event.data.get('message', '')}[/red]")

        except KeyboardInterrupt:
            agent.cancel()
            if live is not None:
                live.stop()
                live = None
            text_buffer.clear()
            console.print("\n[dim]cancelled[/dim]")
            continue

        if live is not None:
            live.stop()
            live = None
        text_buffer.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="mycode CLI")
    parser.add_argument("--provider", metavar="NAME", help="Provider name from resolved config")
    parser.add_argument("--model", metavar="MODEL", help="Model name (overrides resolved default)")
    parser.add_argument("--session", metavar="ID", help="Session id (default: per-cwd hash)")
    parser.add_argument("--once", metavar="MESSAGE", help="Run one prompt and exit")
    args = parser.parse_args()

    cwd = os.getcwd()
    settings = get_settings(cwd)

    # Validate provider name before resolving
    if args.provider and args.provider not in settings.providers:
        available = ", ".join(settings.providers.keys()) or "(none configured)"
        console.print(f"[red]unknown provider {args.provider!r}. available: {available}[/red]")
        return

    resolved = resolve_provider(settings, provider_name=args.provider, model=args.model)

    store = SessionStore()
    session_id = args.session or hashlib.sha1(cwd.encode()).hexdigest()[:12]

    data = asyncio.run(store.get_or_create(session_id, model=resolved.model, cwd=cwd, api_base=resolved.api_base))
    messages = data.get("messages") or []

    agent = Agent(
        model=resolved.model,
        provider=resolved.provider_type,
        cwd=cwd,
        session_dir=store.session_dir(session_id),
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        messages=messages,
        settings=settings,
    )

    # Header
    console.print()
    t = Text()
    t.append("mycode", style="bold")
    t.append(" │ ", style="dim")
    t.append(f"{resolved.model}", style="default")
    t.append(f" ({args.provider or settings.default_provider or resolved.provider_type})", style="dim")
    t.append(" │ ", style="dim")
    t.append(cwd, style="dim")
    console.print(t)
    console.print()

    if args.once:
        code = asyncio.run(run_once(agent, store=store, session_id=session_id, message=args.once))
        raise SystemExit(code)

    asyncio.run(chat_loop(agent, store=store, session_id=session_id))


if __name__ == "__main__":
    main()
