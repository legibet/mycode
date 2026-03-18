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
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from mycode.core.agent import Agent
from mycode.core.config import get_settings, resolve_provider
from mycode.core.session import SessionStore

console = Console(highlight=False)

_HISTORY_FILE = os.path.join(os.path.dirname(__file__), ".cli_history")
_PROMPT = ANSI("\033[1m\033[34m❯\033[0m ")
_MAX_WIDTH = 88


def _sep() -> None:
    width = min(shutil.get_terminal_size().columns, _MAX_WIDTH)
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
        first += f"  (+{len(lines) - 1} lines)"
    elif len(lines[0]) > 72:
        first += "…"
    return first


# ---------------------------------------------------------------------------
# TUI Renderer
# ---------------------------------------------------------------------------


class TUIRenderer:
    """Encapsulates rendering logic for streaming agent output.

    Two modes:
    - live_mode=True  (interactive chat): Rich Live for markdown + spinner
    - live_mode=False (--once):           raw text, no Live
    """

    def __init__(self, con: Console, *, live_mode: bool = True):
        self._con = con
        self._live_mode = live_mode
        self._live: Live | None = None
        self._reasoning: list[str] = []
        self._text: list[str] = []

    # -- public API --

    def start(self) -> None:
        """Show spinner while waiting for first token."""
        if self._live_mode:
            self._ensure_live()

    def reasoning(self, chunk: str) -> None:
        if not self._live_mode:
            return
        self._reasoning.append(chunk)
        self._ensure_live()
        self._update()

    def text(self, chunk: str) -> None:
        if self._live_mode:
            self._text.append(chunk)
            self._ensure_live()
            self._update()
        elif chunk:
            self._con.print(chunk, end="", markup=False, highlight=False)

    def tool_start(self, name: str, args: dict) -> None:
        self._flush()
        if not self._live_mode:
            self._con.print()
        preview = _tool_preview(args)
        t = Text()
        t.append("⏺ ", style="green")
        t.append(name.capitalize(), style="bold green")
        if preview:
            t.append(f" {preview}", style="dim")
        self._con.print(t)

    def tool_output(self, line: str) -> None:
        if line:
            t = Text("  │ ", style="dim")
            t.append(line, style="dim")
            self._con.print(t)

    def tool_done(self, result: str) -> None:
        preview = _result_preview(result)
        style = "red" if result.startswith("error") else "dim"
        t = Text("  ⎿ ", style=style)
        t.append(preview, style=style)
        self._con.print(t)

    def error(self, message: str) -> None:
        self._flush()
        t = Text("✕ ", style="red")
        t.append(message, style="red")
        self._con.print(t)

    def finish(self) -> None:
        self._flush()
        if not self._live_mode:
            self._con.print()

    def cancel(self) -> None:
        self._flush()
        self._con.print("[dim]cancelled[/dim]")

    # -- internal --

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(
                self._renderable(),
                console=self._con,
                refresh_per_second=12,
            )
            self._live.start()

    def _update(self) -> None:
        if self._live is not None:
            self._live.update(self._renderable())

    def _flush(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._reasoning.clear()
        self._text.clear()

    def _renderable(self):
        if not self._reasoning and not self._text:
            return Spinner("dots", style="dim")
        parts = []
        if self._reasoning:
            r = Text()
            r.append("Thinking\n", style="dim bold")
            r.append("".join(self._reasoning), style="dim")
            parts.append(r)
        if self._text:
            parts.append(Markdown("".join(self._text)))
        return Group(*parts) if len(parts) > 1 else parts[0]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def run_once(agent: Agent, *, store: SessionStore, session_id: str, message: str) -> int:
    async def on_persist(payload: dict) -> None:
        await store.append_message(session_id, payload)

    renderer = TUIRenderer(console, live_mode=False)
    exit_code = 0

    async for event in agent.achat(message, on_persist=on_persist):
        match event.type:
            case "reasoning":
                pass
            case "text":
                renderer.text(event.data.get("content", ""))
            case "tool_start":
                renderer.tool_start(event.data.get("name", ""), event.data.get("args") or {})
            case "tool_output":
                renderer.tool_output(event.data.get("content", ""))
            case "tool_done":
                result = event.data.get("result", "")
                renderer.tool_done(result)
                if result.startswith("error"):
                    exit_code = 1
            case "error":
                exit_code = 1
                renderer.error(event.data.get("message", ""))

    renderer.finish()
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

        renderer = TUIRenderer(console)
        renderer.start()

        try:
            async for event in agent.achat(user_input, on_persist=on_persist):
                match event.type:
                    case "reasoning":
                        renderer.reasoning(event.data.get("content", ""))
                    case "text":
                        renderer.text(event.data.get("content", ""))
                    case "tool_start":
                        renderer.tool_start(event.data.get("name", ""), event.data.get("args") or {})
                    case "tool_output":
                        renderer.tool_output(event.data.get("content", ""))
                    case "tool_done":
                        renderer.tool_done(event.data.get("result", ""))
                    case "error":
                        renderer.error(event.data.get("message", ""))

        except KeyboardInterrupt:
            agent.cancel()
            renderer.cancel()
            continue

        renderer.finish()


def main() -> None:
    parser = argparse.ArgumentParser(description="mycode CLI")
    parser.add_argument("--provider", metavar="NAME", help="Provider name from resolved config")
    parser.add_argument("--model", metavar="MODEL", help="Model name (overrides resolved default)")
    parser.add_argument("--session", metavar="ID", help="Session id (default: per-cwd hash)")
    parser.add_argument("--once", metavar="MESSAGE", help="Run one prompt and exit")
    args = parser.parse_args()

    cwd = os.getcwd()
    settings = get_settings(cwd)

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
        reasoning_effort=resolved.reasoning_effort,
    )

    # Header
    console.print()
    t = Text()
    t.append("mycode", style="bold")
    t.append(" ── ", style="dim")
    t.append(resolved.model)
    console.print(t)

    if args.once:
        code = asyncio.run(run_once(agent, store=store, session_id=session_id, message=args.once))
        raise SystemExit(code)

    asyncio.run(chat_loop(agent, store=store, session_id=session_id))


if __name__ == "__main__":
    main()
