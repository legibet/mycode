"""Rendering helpers for the terminal CLI."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Heading as _RichHeading
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from mycode.core.agent import Agent

from .theme import (
    ACCENT,
    CODE_THEME,
    ERROR,
    ERROR_MARKER,
    MUTED,
    PROVIDER,
    STATS,
    SUCCESS,
    TERMINAL_THEME,
    THINKING,
    THINKING_SYMBOL,
    TOOL_BORDER,
    TOOL_END,
    TOOL_MARKER,
    TOOL_NAME,
    WARNING,
)

# In light mode, Rich's default inline-code style ("bold cyan on black") is
# unreadable. Override both inline and indented-block code styles.
_LIGHT_THEME = Theme(
    {
        "markdown.code": "bold blue",
        "markdown.code_block": "blue",
    }
)

console = Console(highlight=False, theme=_LIGHT_THEME if TERMINAL_THEME == "light" else None)


class _LeftHeading(_RichHeading):
    """Heading variant that left-aligns all heading levels."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text
        text.justify = "left"
        if self.tag == "h1":
            yield Text("")
            yield text
            yield Text("")
        elif self.tag == "h2":
            yield Text("")
            yield text
        else:
            yield text


class _LeftMarkdown(Markdown):
    """Markdown subclass with left-aligned headings."""

    elements = {**Markdown.elements, "heading_open": _LeftHeading}


# Maps built-in tool names to the argument key most useful as a one-line preview.
_TOOL_PREVIEW_KEY: dict[str, str] = {
    "read": "path",
    "write": "path",
    "edit": "path",
    "bash": "command",
}


def _format_usage(usage: dict[str, Any]) -> str:
    """Format token usage into a compact string."""
    input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    total = input_t + output_t
    if total:
        return f"{total:,} tokens"
    return ""


class TerminalView:
    """Print static CLI output such as headers, previews, and session lists."""

    def __init__(self, output: Console | None = None) -> None:
        self.console = output or console

    def print_header(
        self, *, provider: str, model: str, session: dict[str, Any], mode: str, message_count: int
    ) -> None:
        """Print the current session header shown above the interactive chat."""

        self.console.print()

        title = session.get("title") or ""
        session_id = str(session.get("id") or "")[:8]

        line = Text()
        line.append("mycode", style=ACCENT)
        line.append("  ")
        line.append(provider, style=PROVIDER)
        line.append("/", style=MUTED)
        line.append(model)
        if session_id:
            line.append("  ")
            line.append(session_id, style=MUTED)
        self.console.print(line)

        if mode == "resumed":
            meta = Text()
            meta.append("resumed", style=WARNING)
            if title and title != "New chat":
                meta.append("  ")
                meta.append(title, style=MUTED)
            if message_count:
                meta.append(f"  ({message_count} msgs)", style=MUTED)
            self.console.print(meta)

        self.console.rule(style="dim")

    def print_history_preview(self, messages: list[dict[str, Any]]) -> None:
        """Print a short summary of recent messages for resumed sessions."""

        entries = self.history_preview_entries(messages)
        if not entries:
            return

        self.console.print(Text(f"recent ({len(entries)})", style=MUTED))
        for role, content in entries:
            label = "you" if role == "You" else "assistant"
            line = Text()
            line.append(f"{label} ", style=MUTED)
            line.append(content)
            self.console.print(line)

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
            self.console.print(Text("no sessions found", style=MUTED))
            return

        self.console.print(Text(f"{heading} ({len(sessions)})", style=MUTED))
        self.console.print()

        title_limit = 24 if include_cwd else 40
        model_limit = 18 if include_cwd else 24
        cwd_limit = 32 if include_cwd else 48

        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), expand=False)
        table.add_column(no_wrap=True)  # marker
        table.add_column(no_wrap=True)  # index
        table.add_column(no_wrap=True)  # session id
        table.add_column(no_wrap=True)  # timestamp
        table.add_column()  # title
        table.add_column(no_wrap=True)  # model
        if include_cwd:
            table.add_column()  # cwd

        for index, session in enumerate(sessions, start=1):
            session_id = str(session.get("id") or "-")
            is_current = bool(current_session_id and session_id == current_session_id)

            marker = Text("●", style=SUCCESS) if is_current else Text(" ")
            idx = Text(str(index), style=MUTED)
            sid = Text(session_id[:12], style=MUTED)
            ts = Text(self._format_timestamp(str(session.get("updated_at") or "")), style=MUTED)
            title = Text(self._shorten(str(session.get("title") or "New chat"), limit=title_limit))

            model = str(session.get("model") or "")
            model_text = Text(
                f"[{self._shorten(model, limit=model_limit)}]" if model else "",
                style=MUTED,
            )

            row: list[Any] = [marker, idx, sid, ts, title, model_text]
            if include_cwd:
                cwd = str(session.get("cwd") or "")
                row.append(Text(self._shorten(cwd, limit=cwd_limit), style=MUTED))

            table.add_row(*row)

        self.console.print(table)

    def history_preview_entries(self, messages: list[dict[str, Any]], *, limit: int = 6) -> list[tuple[str, str]]:
        """Return the compact history preview used for resumed sessions."""

        entries: list[tuple[str, str]] = []

        for message in messages:
            entry = self._history_preview_entry(message)
            if entry is not None:
                entries.append(entry)

        return entries if limit <= 0 else entries[-limit:]

    def _history_preview_entry(self, message: dict[str, Any]) -> tuple[str, str] | None:
        """Build one compact preview entry for a stored conversation message."""

        role = message.get("role")
        content = message.get("content")

        if role == "user":
            text = self._message_text(content)
            if text:
                return ("You", self._shorten(text))
            return None

        if role != "assistant":
            return None

        return self._assistant_history_entry(content)

    def _assistant_history_entry(self, content: Any) -> tuple[str, str] | None:
        """Summarize one assistant message for the session preview."""

        text = ""
        thinking = ""
        tool_names: list[str] = []

        if isinstance(content, list):
            text = " ".join(str(block.get("text") or "").strip() for block in content if block.get("type") == "text")
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
            return ("Assistant", f"{text}{tools_suffix}")

        if thinking:
            return ("Assistant", f"Thinking: {thinking}{tools_suffix}")

        if not tool_names:
            return None

        preview = ", ".join(tool_names[:3])
        if len(tool_names) > 3:
            preview += f" +{len(tool_names) - 3}"
        return ("Assistant", f"[Used tools: {preview}]")

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
        # Timing & stats
        self._response_start_time: float | None = None
        self._thinking_start_time: float | None = None
        self._thinking_collapsed = False
        self._tool_start_time: float | None = None
        self._usage: dict[str, Any] | None = None

    async def render(
        self,
        agent: Agent,
        message: str,
        *,
        on_persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> int:
        """Stream one assistant turn to the terminal and return its exit code."""

        exit_code = 0
        self._response_start_time = time.monotonic()

        async def _tracking_persist(msg: dict[str, Any]) -> None:
            if msg.get("role") == "assistant":
                meta = msg.get("meta") or {}
                usage = meta.get("usage")
                if usage:
                    self._usage = usage
            if on_persist:
                await on_persist(msg)

        if self._live_mode:
            self._ensure_live()

        async for event in agent.achat(message, on_persist=_tracking_persist):
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
        """Handle one streamed reasoning chunk from the agent."""

        if self._thinking_start_time is None:
            self._thinking_start_time = time.monotonic()
        self._reasoning.append(chunk)
        if self._live_mode:
            self._ensure_live()
            self._update()

    def text(self, chunk: str) -> None:
        """Handle one streamed assistant text chunk."""

        self._finalize_reasoning_phase()
        if self._live_mode:
            self._text.append(chunk)
            self._ensure_live()
            self._update()
        elif chunk:
            self._console.print(chunk, end="", markup=False, highlight=False)

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        """Render the start of a tool call."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()
        if not self._live_mode:
            self._console.print()

        self._tool_start_time = time.monotonic()

        preview = ""
        if args:
            key = _TOOL_PREVIEW_KEY.get(name.lower())
            raw = args.get(key) if key else next(iter(args.values()), "")
            preview = str(raw or "")
            if len(preview) > 60:
                preview = preview[:60] + "…"

        text = Text()
        text.append(f"{TOOL_MARKER} ", style=SUCCESS)
        text.append(name.capitalize(), style=TOOL_NAME)
        if preview:
            text.append(f"  {preview}", style=MUTED)
        self._console.print(text)

    def tool_output(self, line: str) -> None:
        """Render one streamed output line from a running tool."""

        if not line:
            return
        text = Text(f"  {TOOL_BORDER} ", style=MUTED)
        text.append(line, style=MUTED)
        self._console.print(text)

    def tool_done(self, result: str) -> None:
        """Render the final tool result preview."""

        lines = result.splitlines()
        preview = ""
        if lines:
            preview = lines[0][:72]
            if len(lines) > 1:
                preview += f"  (+{len(lines) - 1} lines)"
            elif len(lines[0]) > 72:
                preview += "…"

        is_error = result.startswith("error")
        style = ERROR if is_error else MUTED

        duration = ""
        if self._tool_start_time is not None:
            elapsed = time.monotonic() - self._tool_start_time
            if elapsed >= 0.5:
                duration = f" ({elapsed:.1f}s)"
            self._tool_start_time = None

        text = Text(f"  {TOOL_END} ", style=style)
        text.append(preview, style=style)
        if duration:
            text.append(duration, style=STATS)
        self._console.print(text)

    def error(self, message: str) -> None:
        """Render a terminal-visible error message for the current turn."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()
        text = Text(f"{ERROR_MARKER} ", style=ERROR)
        text.append(message, style=ERROR)
        self._console.print(text)

    def cancel(self) -> None:
        """Render a cancellation marker and reset transient state."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()
        self._console.print(Text("cancelled", style=MUTED))

    def finish(self) -> None:
        """Flush the current turn and print timing or token statistics."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()

        parts: list[str] = []
        if self._response_start_time is not None:
            elapsed = time.monotonic() - self._response_start_time
            parts.append(f"{elapsed:.1f}s")
        if self._usage:
            token_str = _format_usage(self._usage)
            if token_str:
                parts.append(token_str)

        if parts:
            self._console.print(Text("  " + " · ".join(parts), style=STATS))

        if not self._live_mode:
            self._console.print()

    # -- Internal helpers ----------------------------------------------------

    def _finalize_reasoning_phase(self) -> None:
        """Finish the reasoning phase before text, tools, or final output."""

        if self._live_mode:
            self._collapse_thinking()
        else:
            self._print_static_reasoning()

    def _collapse_thinking(self) -> None:
        """In live mode: stop the spinner and print a one-line summary."""
        if self._thinking_collapsed or not self._reasoning:
            return
        self._thinking_collapsed = True

        if self._live is not None:
            self._live.stop()
            self._live = None

        duration = ""
        if self._thinking_start_time is not None:
            elapsed = time.monotonic() - self._thinking_start_time
            duration = f" · {elapsed:.1f}s"

        self._console.print(Text(f"{THINKING_SYMBOL} thought{duration}", style=THINKING))
        self._reasoning.clear()

    def _print_static_reasoning(self) -> None:
        """Non-live mode: print full reasoning content."""
        if self._live_mode or self._printed_static_reasoning or not self._reasoning:
            return

        duration = ""
        if self._thinking_start_time is not None:
            elapsed = time.monotonic() - self._thinking_start_time
            duration = f" · {elapsed:.1f}s"

        self._console.print(Text(f"{THINKING_SYMBOL} thinking{duration}", style=THINKING))
        self._console.print("".join(self._reasoning), style="dim")
        self._printed_static_reasoning = True

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(self._build_live_renderable(), console=self._console, refresh_per_second=12)
            self._live.start()

    def _update(self) -> None:
        if self._live is not None:
            self._live.update(self._build_live_renderable())

    def _reset_stream_state(self) -> None:
        """Stop live rendering and clear transient buffers for the current phase."""

        if self._live is not None:
            self._live.stop()
            self._live = None
        self._reasoning.clear()
        self._text.clear()
        self._printed_static_reasoning = False
        self._thinking_collapsed = False
        self._thinking_start_time = None

    def _build_live_renderable(self):
        """Build the Rich renderable used while a reply is streaming."""

        # No content yet: plain spinner
        if not self._reasoning and not self._text:
            return Spinner("dots", style="dim")

        # Thinking in progress: show rolling preview of reasoning content
        if self._reasoning and not self._text:
            content = " ".join("".join(self._reasoning).split())
            if content:
                preview = content[-80:].strip()
                if len(content) > 80:
                    preview = "…" + preview
                return Spinner("dots", text=Text(f" {preview}", style=THINKING), style="dim")
            return Spinner("dots", text=Text(" thinking…", style=THINKING), style="dim")

        # Text streaming: render as markdown (thinking already collapsed)
        if self._text:
            return _LeftMarkdown("".join(self._text), code_theme=CODE_THEME)

        return Spinner("dots", style="dim")
