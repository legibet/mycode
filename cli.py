"""CLI for mycode.

This is intentionally small. The canonical implementation is the backend agent loop.
The CLI reuses the same agent + session store.

Usage:

  uv run python cli.py [--provider NAME] [--model MODEL]

Config:
  Edit config.json to define providers.
  Env var fallback: MODEL (e.g. openai:gpt-4o), BASE_URL, OPENAI_API_KEY / ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
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
from app.config import ProviderConfig, get_settings
from app.session import SessionStore

console = Console(highlight=False)

_HISTORY_FILE = os.path.join(os.path.dirname(__file__), ".cli_history")
_FALLBACK_MODEL = "claude-sonnet-4-5"
_FALLBACK_PROVIDER = "anthropic"


def _tool_preview(args: dict) -> str:
    if not args:
        return ""
    value = str(next(iter(args.values())))
    return value[:60] + "…" if len(value) > 60 else value


async def chat_loop(agent: Agent, *, store: SessionStore, session_id: str) -> None:
    session: PromptSession = PromptSession(history=FileHistory(_HISTORY_FILE))

    async def on_persist(message: dict) -> None:
        await store.append_message(session_id, message)

    while True:
        console.print(Rule(style="dim"))

        try:
            user_input: str = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.prompt("❯ "),
            )
        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            return

        user_input = user_input.strip()
        if not user_input:
            continue

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
                "[dim]/c[/dim]  clear conversation\n"
                "[dim]/q[/dim]  quit\n"
                "[dim]/model <name>[/dim]  switch model\n"
                "[dim]/history[/dim]  show last messages\n"
                "[dim]Ctrl-C[/dim]  cancel current request"
            )
            continue

        if user_input.startswith("/model "):
            new_model = user_input[len("/model ") :].strip()
            if not new_model:
                console.print("[red]Usage:[/red] /model <name>")
                continue
            agent.model = new_model
            console.print(f"[green]✓[/green] Model switched to [cyan]{new_model}[/cyan]")
            continue

        if user_input == "/history":
            recent = [m for m in agent.messages if m.get("role") in {"user", "assistant"}][-10:]
            if not recent:
                console.print("[dim](no messages)[/dim]")
                continue

            for i, msg in enumerate(recent, 1):
                role = msg.get("role", "unknown")
                content = str(msg.get("content") or "").replace("\n", " ").strip()
                preview = content[:80] + ("…" if len(content) > 80 else "")
                console.print(f"[dim]{i:>2}[/dim] [{role}] {preview or '(tool-only message)'}")
            continue

        if user_input.startswith("/"):
            console.print("[red]Unknown command.[/red] Try [dim]/h[/dim].")
            continue

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

        if live is not None:
            live.stop()
            live = None
        text_buffer.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="mycode CLI")
    parser.add_argument("--provider", metavar="NAME", help="Provider name from config.json")
    parser.add_argument("--model", metavar="MODEL", help="Model name (overrides config default)")
    args = parser.parse_args()

    settings = get_settings()

    # Resolve provider config
    cfg: ProviderConfig | None = None
    if args.provider:
        cfg = settings.providers.get(args.provider)
        if cfg is None:
            available = ", ".join(settings.providers.keys()) or "(none configured)"
            console.print(f"[red]Unknown provider:[/red] {args.provider!r}. Available: {available}")
            return
    else:
        cfg = settings.active_provider

    # Resolve model: CLI flag > config default > first model of provider > fallback
    model = args.model or settings.default_model or (cfg.models[0] if cfg and cfg.models else None) or _FALLBACK_MODEL
    provider_type = cfg.type if cfg else _FALLBACK_PROVIDER
    api_base = cfg.base_url if cfg else None
    api_key = cfg.api_key if cfg else None

    cwd = os.getcwd()
    store = SessionStore()
    session_id = hashlib.sha1(cwd.encode()).hexdigest()[:12]

    data = asyncio.run(store.get_or_create(session_id, model=model, cwd=cwd, api_base=api_base))
    messages = data.get("messages") or []

    agent = Agent(
        model=model,
        provider=provider_type,
        cwd=cwd,
        session_dir=store.session_dir(session_id),
        api_key=api_key,
        api_base=api_base,
        messages=messages,
    )

    # Header
    console.print()
    header = Text()
    header.append("mycode", style="bold")
    header.append("  ")
    if cfg:
        header.append(cfg.name, style="green")
        header.append("/", style="dim")
    header.append(model, style="cyan")
    header.append("  ")
    header.append(cwd, style="dim")
    console.print(header)
    console.print("[dim]/h help  /c clear  /q quit  /model <name>  /history  Ctrl-C cancel[/dim]")

    asyncio.run(chat_loop(agent, store=store, session_id=session_id))


if __name__ == "__main__":
    main()
