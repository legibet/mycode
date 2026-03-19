"""Rendering helpers for the terminal CLI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from mycode.core.agent import Agent

console = Console(highlight=False)


class TerminalView:
    """Print static CLI output such as headers, previews, and session lists."""

    def __init__(self, output: Console | None = None) -> None:
        self.console = output or console

    def print_rule(self, *, width: int) -> None:
        self.console.print(f"[dim]{'─' * width}[/dim]")

    def print_header(
        self, *, provider: str, model: str, session: dict[str, Any], mode: str, message_count: int
    ) -> None:
        """Print the current session header shown above the interactive chat."""

        self.console.print()

        title = session.get("title") or "New chat"
        session_id = str(session.get("id") or "")[:12]

        title_text = Text()
        title_text.append("mycode", style="bold")
        title_text.append(" -- ", style="dim")
        title_text.append(provider, style="cyan")
        title_text.append(" / ", style="dim")
        title_text.append(model)
        self.console.print(title_text)

        meta_text = Text()
        meta_text.append("session ", style="dim")
        meta_text.append(session_id or "-", style="bold")
        meta_text.append("  ")
        meta_text.append(mode, style="green" if mode == "new" else "yellow")
        meta_text.append("  ")
        meta_text.append(title)
        if message_count:
            meta_text.append(f"  ({message_count} stored messages)", style="dim")
        self.console.print(meta_text)

    def print_history_preview(self, messages: list[dict[str, Any]]) -> None:
        """Print a short summary of recent messages for resumed sessions."""

        entries = self.history_preview_entries(messages)
        if not entries:
            return

        self.console.print(f"[dim]history preview (showing last {len(entries)})[/dim]")
        for role, content in entries:
            label = "user" if role == "You" else "assistant"
            self.console.print(f"[dim]{label}[/dim] {content}")

    def print_session_list(
        self,
        sessions: list[dict[str, Any]],
        *,
        include_cwd: bool = False,
        current_session_id: str | None = None,
        heading: str = "sessions",
    ) -> None:
        """Print saved sessions in a compact table for selection commands."""

        if not sessions:
            self.console.print("[dim]no sessions found[/dim]")
            return

        self.console.print(f"[dim]{heading} ({len(sessions)})[/dim]")
        for index, session in enumerate(sessions, start=1):
            parts: list[str] = []
            session_id = str(session.get("id") or "-")
            marker = "*" if current_session_id and session_id == current_session_id else " "
            title_limit = 24 if include_cwd else 40
            model_limit = 18 if include_cwd else 24
            cwd_limit = 32 if include_cwd else 48

            parts.append(f"{marker}{index:>2}")
            parts.append(session_id[:12])
            parts.append(self._format_timestamp(str(session.get("updated_at") or "")))
            parts.append(self._shorten(str(session.get("title") or "New chat"), limit=title_limit))

            model = str(session.get("model") or "")
            if model:
                parts.append(f"[{self._shorten(model, limit=model_limit)}]")

            if include_cwd:
                cwd = str(session.get("cwd") or "")
                if cwd:
                    parts.append(self._shorten(cwd, limit=cwd_limit))

            self.console.print("  ".join(parts))

    def history_preview_entries(self, messages: list[dict[str, Any]], *, limit: int = 6) -> list[tuple[str, str]]:
        """Return the compact history preview used for resumed sessions."""

        entries: list[tuple[str, str]] = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role == "user":
                text = self._message_text(content)
                if text:
                    entries.append(("You", self._shorten(text)))
                continue

            if role != "assistant":
                continue

            text = ""
            thinking = ""
            tool_names: list[str] = []
            if isinstance(content, list):
                text = " ".join(
                    str(block.get("text") or "").strip() for block in content if block.get("type") == "text"
                )
                thinking = " ".join(
                    str(block.get("text") or "").strip() for block in content if block.get("type") == "thinking"
                )
                tool_names = [str(block.get("name") or "tool") for block in content if block.get("type") == "tool_use"]
            else:
                text = str(content or "")

            text = self._shorten(text)
            thinking = self._shorten(thinking)
            tools_suffix = f"  [{len(tool_names)} tool{'s' if len(tool_names) != 1 else ''}]" if tool_names else ""

            if text:
                entries.append(("Assistant", f"{text}{tools_suffix}"))
                continue

            if thinking:
                entries.append(("Assistant", f"Thinking: {thinking}{tools_suffix}"))
                continue

            if tool_names:
                preview = ", ".join(tool_names[:3])
                if len(tool_names) > 3:
                    preview += f" +{len(tool_names) - 3}"
                entries.append(("Assistant", f"[Used tools: {preview}]"))

        return entries if limit <= 0 else entries[-limit:]

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, list):
            return " ".join(str(block.get("text") or "").strip() for block in content if block.get("type") == "text")
        return str(content or "")

    @staticmethod
    def _shorten(value: str, *, limit: int = 96) -> str:
        text = " ".join((value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @staticmethod
    def _format_timestamp(value: str) -> str:
        if not value:
            return "-"
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value[:16].replace("T", " ")


class ReplyRenderer:
    """Render one assistant reply, including thinking and tool output."""

    def __init__(self, output: Console | None = None, *, live_mode: bool = True) -> None:
        self._console = output or console
        self._live_mode = live_mode
        self._live: Live | None = None
        self._reasoning: list[str] = []
        self._text: list[str] = []
        self._printed_static_reasoning = False

    async def render(
        self,
        agent: Agent,
        message: str,
        *,
        on_persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> int:
        """Stream one assistant turn to the terminal and return its exit code."""

        exit_code = 0
        if self._live_mode:
            self._ensure_live()

        async for event in agent.achat(message, on_persist=on_persist):
            match event.type:
                case "reasoning":
                    self.reasoning(event.data.get("delta", ""))
                case "text":
                    self.text(event.data.get("delta", ""))
                case "tool_start":
                    tool_call = event.data.get("tool_call") or {}
                    self.tool_start(tool_call.get("name", ""), tool_call.get("input") or {})
                case "tool_output":
                    self.tool_output(event.data.get("output", ""))
                case "tool_done":
                    result = event.data.get("result", "")
                    self.tool_done(result)
                    if event.data.get("is_error") or result.startswith("error"):
                        exit_code = 1
                case "error":
                    exit_code = 1
                    self.error(event.data.get("message", ""))

        self.finish()
        return exit_code

    def reasoning(self, chunk: str) -> None:
        self._reasoning.append(chunk)
        if self._live_mode:
            self._ensure_live()
            self._update()

    def text(self, chunk: str) -> None:
        self._print_static_reasoning()
        if self._live_mode:
            self._text.append(chunk)
            self._ensure_live()
            self._update()
        elif chunk:
            self._console.print(chunk, end="", markup=False, highlight=False)

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        self._print_static_reasoning()
        self._flush()
        if not self._live_mode:
            self._console.print()

        preview = ""
        if args:
            preview = str(next(iter(args.values())))
            if len(preview) > 60:
                preview = preview[:60] + "…"

        text = Text()
        text.append("⏺ ", style="green")
        text.append(name.capitalize(), style="bold green")
        if preview:
            text.append(f" {preview}", style="dim")
        self._console.print(text)

    def tool_output(self, line: str) -> None:
        if not line:
            return
        text = Text("  │ ", style="dim")
        text.append(line, style="dim")
        self._console.print(text)

    def tool_done(self, result: str) -> None:
        lines = result.splitlines()
        preview = ""
        if lines:
            preview = lines[0][:72]
            if len(lines) > 1:
                preview += f"  (+{len(lines) - 1} lines)"
            elif len(lines[0]) > 72:
                preview += "…"

        style = "red" if result.startswith("error") else "dim"
        text = Text("  ⎿ ", style=style)
        text.append(preview, style=style)
        self._console.print(text)

    def error(self, message: str) -> None:
        self._print_static_reasoning()
        self._flush()
        text = Text("✕ ", style="red")
        text.append(message, style="red")
        self._console.print(text)

    def cancel(self) -> None:
        self._print_static_reasoning()
        self._flush()
        self._console.print("[dim]cancelled[/dim]")

    def finish(self) -> None:
        self._print_static_reasoning()
        self._flush()
        if not self._live_mode:
            self._console.print()

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(self._renderable(), console=self._console, refresh_per_second=12)
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
        self._printed_static_reasoning = False

    def _print_static_reasoning(self) -> None:
        if self._live_mode or self._printed_static_reasoning or not self._reasoning:
            return
        self._console.print("Thinking", style="dim bold")
        self._console.print("".join(self._reasoning), style="dim")
        self._printed_static_reasoning = True

    def _renderable(self):
        if not self._reasoning and not self._text:
            return Spinner("dots", style="dim")

        parts = []
        if self._reasoning:
            reasoning = Text()
            reasoning.append("Thinking\n", style="dim bold")
            reasoning.append("".join(self._reasoning), style="dim")
            parts.append(reasoning)
        if self._text:
            parts.append(Markdown("".join(self._text)))
        return Group(*parts) if len(parts) > 1 else parts[0]
