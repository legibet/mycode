"""CLI for mycode.

Usage:
  mycode [--provider NAME] [--model MODEL] [--once MESSAGE]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


@dataclass
class CLIResolvedSession:
    session_id: str
    session: dict[str, Any]
    messages: list[dict[str, Any]]
    mode: str


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


def _compact_text(value: str, *, limit: int = 96) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _history_preview_entries(messages: list[dict[str, Any]], *, limit: int = 6) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []

    for msg in messages:
        role = msg.get("role")

        if role == "user":
            content = _compact_text(str(msg.get("content") or ""))
            if content:
                entries.append(("You", content))
            continue

        if role != "assistant":
            continue

        content = _compact_text(str(msg.get("content") or ""))
        tool_calls = msg.get("tool_calls") or []

        if content:
            if tool_calls:
                content = f"{content}  [{len(tool_calls)} tool{'s' if len(tool_calls) != 1 else ''}]"
            entries.append(("Assistant", content))
            continue

        if tool_calls:
            names = [tc.get("function", {}).get("name") or "tool" for tc in tool_calls]
            preview = ", ".join(names[:3])
            if len(names) > 3:
                preview += f" +{len(names) - 3}"
            entries.append(("Assistant", f"[Used tools: {preview}]"))

    if limit <= 0:
        return entries
    return entries[-limit:]


def _format_timestamp(value: str) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def _session_line(
    session: dict[str, Any],
    *,
    index: int | None = None,
    include_cwd: bool = False,
    current_session_id: str | None = None,
) -> str:
    parts: list[str] = []
    session_id = str(session.get("id") or "-")
    is_current = bool(current_session_id and session_id == current_session_id)
    title_limit = 24 if include_cwd else 40
    model_limit = 18 if include_cwd else 24
    cwd_limit = 32 if include_cwd else 48

    if index is not None:
        marker = "*" if is_current else " "
        parts.append(f"{marker}{index:>2}")

    parts.append(session_id[:12])
    parts.append(_format_timestamp(str(session.get("updated_at") or "")))
    parts.append(_compact_text(str(session.get("title") or "New chat"), limit=title_limit))

    model = str(session.get("model") or "")
    if model:
        parts.append(f"[{_compact_text(model, limit=model_limit)}]")

    if include_cwd:
        cwd = str(session.get("cwd") or "")
        if cwd:
            parts.append(_compact_text(cwd, limit=cwd_limit))

    return "  ".join(parts)


def _print_session_list(
    sessions: list[dict[str, Any]],
    *,
    include_cwd: bool = False,
    current_session_id: str | None = None,
    heading: str = "sessions",
) -> None:
    if not sessions:
        console.print("[dim]no sessions found[/dim]")
        return

    console.print(f"[dim]{heading} ({len(sessions)})[/dim]")
    for index, session in enumerate(sessions, start=1):
        console.print(
            _session_line(session, index=index, include_cwd=include_cwd, current_session_id=current_session_id)
        )


def _resolve_session_choice(choice: str, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    value = choice.strip()
    if not value:
        return None

    if value.isdigit():
        idx = int(value) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
        return None

    matches = [session for session in sessions if str(session.get("id") or "").startswith(value)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous session id: {value}")
    return None


def _print_header(*, model: str, session: dict[str, Any], mode: str, message_count: int) -> None:
    console.print()
    title = session.get("title") or "New chat"
    session_id = str(session.get("id") or "")[:12]

    title_text = Text()
    title_text.append("mycode", style="bold")
    title_text.append(" ── ", style="dim")
    title_text.append(model)
    console.print(title_text)

    meta_text = Text()
    meta_text.append("session ", style="dim")
    meta_text.append(session_id or "-", style="bold")
    meta_text.append("  ")
    meta_text.append(mode, style="green" if mode == "new" else "yellow")
    meta_text.append("  ")
    meta_text.append(title)
    if message_count:
        meta_text.append(f"  ({message_count} stored messages)", style="dim")
    console.print(meta_text)


def _print_history_preview(messages: list[dict[str, Any]]) -> None:
    entries = _history_preview_entries(messages)
    if not entries:
        return

    console.print(f"[dim]history preview (showing last {len(entries)})[/dim]")
    for role, content in entries:
        label = "user" if role == "You" else "assistant"
        console.print(f"[dim]{label}[/dim] {content}")


def _clone_agent(agent: Agent, *, session_dir: Path, messages: list[dict[str, Any]]) -> Agent:
    return Agent(
        model=agent.model,
        provider=agent.provider,
        cwd=agent.cwd,
        session_dir=session_dir,
        api_key=agent.api_key,
        api_base=agent.api_base,
        messages=messages,
        max_turns=agent.max_turns,
        max_tokens=agent.max_tokens,
        reasoning_effort=agent.reasoning_effort,
        settings=agent.settings,
    )


async def resolve_cli_session(
    *,
    store: SessionStore,
    cwd: str,
    model: str,
    api_base: str | None,
    requested_session_id: str | None,
    continue_last: bool,
) -> CLIResolvedSession:
    if requested_session_id:
        data = await store.load_session(requested_session_id)
        if not data or not data.get("session"):
            raise ValueError(f"Unknown session: {requested_session_id}")

        synced = await store.get_or_create(requested_session_id, model=model, cwd=cwd, api_base=api_base)
        session = synced.get("session") or data["session"]
        messages = synced.get("messages") or data.get("messages") or []
        return CLIResolvedSession(
            session_id=requested_session_id,
            session=session,
            messages=messages,
            mode="resumed",
        )

    if continue_last:
        latest = await store.latest_session(cwd=cwd)
        if latest and latest.get("id"):
            session_id = str(latest["id"])
            data = await store.get_or_create(session_id, model=model, cwd=cwd, api_base=api_base)
            return CLIResolvedSession(
                session_id=session_id,
                session=data.get("session") or latest,
                messages=data.get("messages") or [],
                mode="resumed",
            )

    data = await store.create_session(None, model=model, cwd=cwd, api_base=api_base)
    session = data.get("session") or {}
    return CLIResolvedSession(
        session_id=str(session.get("id") or ""),
        session=session,
        messages=[],
        mode="new",
    )


async def list_cli_sessions(*, store: SessionStore, cwd: str, show_all: bool) -> list[dict[str, Any]]:
    return await store.list_sessions(cwd=None if show_all else cwd)


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
    active_session_id = session_id

    async def on_persist(message: dict) -> None:
        await store.append_message(active_session_id, message)

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
            await store.clear_session(active_session_id)
            agent.clear()
            console.print("[green]⏺[/green] [dim]cleared[/dim]")
            continue

        if user_input == "/new":
            data = await store.create_session(None, model=agent.model, cwd=agent.cwd, api_base=agent.api_base)
            new_session = data.get("session") or {}
            active_session_id = str(new_session.get("id") or "")
            agent = _clone_agent(agent, session_dir=store.session_dir(active_session_id), messages=[])
            _print_header(model=agent.model, session=new_session, mode="new", message_count=0)
            continue

        if user_input == "/resume":
            sessions = await store.list_sessions(cwd=agent.cwd)
            sessions = [item for item in sessions if item.get("id") != active_session_id]
            if not sessions:
                console.print("[dim]no other sessions in this workspace[/dim]")
                continue

            _print_session_list(
                sessions,
                current_session_id=active_session_id,
                heading="resume session: enter number, session id prefix, or blank to cancel",
            )

            while True:
                try:
                    selection = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: session.prompt(ANSI("\033[1mresume>\033[0m ")),
                    )
                except KeyboardInterrupt:
                    selection = ""
                except EOFError:
                    selection = ""

                selection = selection.strip()
                if not selection:
                    console.print("[dim]resume cancelled[/dim]")
                    break

                try:
                    selected = _resolve_session_choice(selection, sessions)
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue

                if not selected:
                    console.print("[dim]unknown session selection[/dim]")
                    continue

                active_session_id = str(selected.get("id") or "")
                data = await store.get_or_create(
                    active_session_id,
                    model=agent.model,
                    cwd=agent.cwd,
                    api_base=agent.api_base,
                )
                messages = data.get("messages") or []
                resumed_session = data.get("session") or selected
                agent = _clone_agent(
                    agent,
                    session_dir=store.session_dir(active_session_id),
                    messages=messages,
                )
                _print_header(
                    model=agent.model,
                    session=resumed_session,
                    mode="resumed",
                    message_count=len(messages),
                )
                _print_history_preview(messages)
                break

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
            console.print("[dim]unknown: /c  /q  /new  /resume  /model <name>[/dim]")
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
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("--session", metavar="ID", help="Resume a specific session id")
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="Resume the most recent session in the current workspace",
    )
    parser.add_argument("--once", metavar="MESSAGE", help="Run one prompt and exit")
    subparsers = parser.add_subparsers(dest="command")
    session_parser = subparsers.add_parser("session", help="Session management commands")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)
    list_parser = session_subparsers.add_parser("list", help="List saved sessions")
    list_parser.add_argument("--all", action="store_true", help="Show sessions from all workspaces")
    args = parser.parse_args()

    cwd = os.getcwd()
    settings = get_settings(cwd)
    store = SessionStore()

    if args.command == "session" and args.session_command == "list":
        sessions = asyncio.run(list_cli_sessions(store=store, cwd=cwd, show_all=args.all))
        heading = "all sessions" if args.all else f"sessions for {cwd}"
        _print_session_list(sessions, include_cwd=args.all, heading=heading)
        return

    if args.provider and args.provider not in settings.providers:
        available = ", ".join(settings.providers.keys()) or "(none configured)"
        console.print(f"[red]unknown provider {args.provider!r}. available: {available}[/red]")
        return

    resolved = resolve_provider(settings, provider_name=args.provider, model=args.model)

    try:
        resolved_session = asyncio.run(
            resolve_cli_session(
                store=store,
                cwd=cwd,
                model=resolved.model,
                api_base=resolved.api_base,
                requested_session_id=args.session,
                continue_last=args.continue_last,
            )
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    agent = Agent(
        model=resolved.model,
        provider=resolved.provider_type,
        cwd=cwd,
        session_dir=store.session_dir(resolved_session.session_id),
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        messages=resolved_session.messages,
        settings=settings,
        reasoning_effort=resolved.reasoning_effort,
    )

    _print_header(
        model=resolved.model,
        session=resolved_session.session,
        mode=resolved_session.mode,
        message_count=len(resolved_session.messages),
    )

    if args.once:
        code = asyncio.run(run_once(agent, store=store, session_id=resolved_session.session_id, message=args.once))
        raise SystemExit(code)

    if resolved_session.mode == "resumed":
        _print_history_preview(resolved_session.messages)

    asyncio.run(chat_loop(agent, store=store, session_id=resolved_session.session_id))


if __name__ == "__main__":
    main()
