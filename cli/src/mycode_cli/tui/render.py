"""Rendering helpers for the terminal CLI."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, override

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import CodeBlock as _RichCodeBlock
from rich.markdown import Heading as _RichHeading
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from mycode.agent import Agent
from mycode.messages import flatten_message_text

from .theme import (
    ACCENT,
    CODE_THEME,
    ERROR,
    ERROR_MARKER,
    MUTED,
    PROMPT_CHAR,
    PROVIDER,
    STATS,
    SUCCESS,
    TERMINAL_THEME,
    THINKING,
    THINKING_SYMBOL,
    TOOL_MARKER,
    TOOL_NAME,
    WARNING,
)

# Override Rich's default inline-code style ("bold cyan on black") to remove
# the hardcoded background color that clashes with terminal themes.
_THEME = Theme(
    {
        "markdown.code": "bold blue" if TERMINAL_THEME == "light" else "bold cyan",
        "markdown.code_block": "blue" if TERMINAL_THEME == "light" else "cyan",
    }
)

console = Console(highlight=False, theme=_THEME)


class _LeftHeading(_RichHeading):
    """Heading variant that left-aligns all heading levels."""

    @override
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


class _CleanCodeBlock(_RichCodeBlock):
    """Code block that uses the terminal background instead of the theme background."""

    @override
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(
            code,
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            padding=0,
            background_color="default",
        )


class _LeftMarkdown(Markdown):
    """Markdown subclass with left-aligned headings and clean code blocks."""

    elements = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
        "fence": _CleanCodeBlock,
        "code_block": _CleanCodeBlock,
    }


_TOOL_OUTPUT_MAX_LINES = 5

# Maps built-in tool names to the argument key most useful as a one-line preview.
_TOOL_PREVIEW_KEY: dict[str, str] = {
    "read": "path",
    "write": "path",
    "edit": "path",
    "bash": "command",
}


def _tool_preview(name: str, args: dict[str, Any]) -> str:
    """Extract a one-line preview string for a tool call."""

    if not args:
        return ""
    key = _TOOL_PREVIEW_KEY.get(name.lower())
    raw = args.get(key) if key else next(iter(args.values()), "")
    preview = str(raw or "")
    if len(preview) > 60:
        preview = preview[:60] + "…"
    return preview


def format_local_timestamp(value: str, display_format: str) -> str:
    """Format an ISO timestamp with a simple local fallback."""

    if not value:
        return ""
    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp.astimezone().strftime(display_format)
    except ValueError:
        return value[:16].replace("T", " ")


def _compact_marker_text(width: int) -> Text:
    """Build the inline ``compacted`` divider used in stream and history views."""

    label = " compacted "
    bar = max(1, (max(width, len(label) + 4) - len(label)) // 2)
    return Text(f"{'─' * bar}{label}{'─' * bar}", style=MUTED)


class TerminalView:
    """Print static CLI output such as headers, previews, and session lists."""

    def __init__(self, output: Console | None = None) -> None:
        self.console = output or console

    def print_header(
        self,
        *,
        provider: str,
        model: str,
        session: dict[str, Any],
        mode: str,
        message_count: int,
        reasoning_effort: str | None = None,
    ) -> None:
        """Print the current session header shown above the interactive chat."""

        self.console.print()

        title = session.get("title") or ""
        session_id = str(session.get("id") or "")[:8]

        line = Text()
        line.append("mycode", style=ACCENT)
        line.append(" · ", style=MUTED)
        line.append(provider, style=PROVIDER)
        line.append(" / ", style=MUTED)
        line.append(model)
        if reasoning_effort:
            line.append(" · ", style=MUTED)
            line.append(reasoning_effort, style=MUTED)
        if session_id:
            line.append(" · ", style=MUTED)
            line.append(session_id, style=MUTED)
        self.console.print(line)

        if mode == "resumed":
            meta = Text()
            meta.append("resumed", style=WARNING)
            if title and title != "New chat":
                meta.append(" · ", style=MUTED)
                meta.append(title, style=MUTED)
            if message_count:
                meta.append(" · ", style=MUTED)
                meta.append(f"{message_count} msgs", style=MUTED)
            self.console.print(meta)

    def print_history_preview(self, messages: list[dict[str, Any]]) -> None:
        """Print recent conversation turns for resumed sessions."""

        turns = self.history_preview_entries(messages)
        if not turns:
            return

        self.console.print(Text("recent", style=MUTED))
        for turn in turns:
            self.console.print()
            for kind, content in turn:
                if kind == "user":
                    lines = str(content).splitlines() or [""]
                    first = Text()
                    first.append(f"{PROMPT_CHAR} ", style=ACCENT)
                    first.append(lines[0])
                    self.console.print(first)
                    for line in lines[1:]:
                        self.console.print(Text(f"  {line}"))
                elif kind == "text":
                    self.console.print(_LeftMarkdown(str(content), code_theme=CODE_THEME))
                elif kind == "compact":
                    self.console.print(_compact_marker_text(self.console.size.width))
                else:
                    name, args = content
                    preview = _tool_preview(name, args if isinstance(args, dict) else {})
                    line = Text()
                    line.append(f"{TOOL_MARKER} ", style=SUCCESS)
                    line.append(name.capitalize(), style=TOOL_NAME)
                    if preview:
                        line.append(f"  {preview}", style=MUTED)
                    self.console.print(line)

    def print_session_list(
        self,
        sessions: list[dict[str, Any]],
        *,
        include_cwd: bool = False,
        heading: str = "sessions",
    ) -> None:
        """Print saved sessions in a compact table for selection commands."""

        if not sessions:
            self.console.print(Text("no sessions found", style=MUTED))
            return

        self.console.print(Text(f"{heading} ({len(sessions)})", style=MUTED))
        self.console.print()

        title_limit = 32 if include_cwd else 48
        cwd_limit = 32 if include_cwd else 48

        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), expand=False)
        table.add_column(no_wrap=True)  # index
        table.add_column(no_wrap=True)  # session id
        table.add_column(no_wrap=True)  # timestamp
        table.add_column()  # title
        if include_cwd:
            table.add_column()  # cwd

        for index, session in enumerate(sessions, start=1):
            session_id = str(session.get("id") or "-")
            idx = Text(str(index), style=MUTED)
            sid = Text(session_id[:12], style=MUTED)
            ts = Text(self._format_timestamp(str(session.get("updated_at") or "")), style=MUTED)
            title = Text(self._shorten(str(session.get("title") or "New chat"), limit=title_limit))

            row: list[Any] = [idx, sid, ts, title]
            if include_cwd:
                cwd = str(session.get("cwd") or "")
                row.append(Text(self._shorten(cwd, limit=cwd_limit), style=MUTED))

            table.add_row(*row)

        self.console.print(table)

    def history_preview_entries(
        self,
        messages: list[dict[str, Any]],
        *,
        limit: int = 3,
    ) -> list[list[tuple[str, Any]]]:
        """Return the last few readable conversation turns for resumed sessions."""

        turns: list[list[tuple[str, Any]]] = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role == "compact":
                turns.append([("compact", None)])
                continue

            if role == "user":
                # Use the shared flattener so attached file payload blocks stay out
                # of the readable history preview.
                text = flatten_message_text(message, include_thinking=False)
                if not isinstance(content, list):
                    text = text or str(content or "").strip()
                if text:
                    turns.append([("user", text)])
                continue

            if role != "assistant":
                continue

            parts: list[tuple[str, Any]] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            parts.append(("text", text))
                    elif block.get("type") == "tool_use":
                        parts.append(("tool", (str(block.get("name") or "tool"), block.get("input"))))
            else:
                text = str(content or "").strip()
                if text:
                    parts.append(("text", text))

            if not parts:
                continue
            if not turns:
                turns.append([])
            turns[-1].extend(parts)

        return turns if limit <= 0 else turns[-limit:]

    @staticmethod
    def _shorten(value: str, *, limit: int = 96) -> str:
        text = " ".join((value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @staticmethod
    def _format_timestamp(value: str) -> str:
        return format_local_timestamp(value, "%Y-%m-%d %H:%M") or "-"


class ReplyRenderer:
    """Render one assistant reply, including thinking and tool output."""

    def __init__(self, output: Console | None = None) -> None:
        self._console = output or console
        self._live: Live | None = None
        self._reasoning: list[str] = []
        self._text: list[str] = []
        self._text_started = False
        # Reasoning & stats
        self._thinking_start_time: float | None = None
        self._thinking_duration_ms: int | None = None
        self._thinking_collapsed = False
        self._had_prior_output = False
        self._tool_output_count = 0
        self._tool_name: str = ""
        self._tool_args: dict[str, Any] = {}
        self._tool_header_printed = False
        self._tool_live: Live | None = None
        # Stats reported by the agent's `usage` event for the latest turn.
        self._stats: dict[str, Any] = {}

    async def render(
        self,
        agent: Agent,
        message: str | dict[str, Any],
        *,
        on_persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> int:
        """Stream one assistant turn to the terminal and return its exit code."""

        exit_code = 0
        self._stats = {}

        self._ensure_live()

        async for event in agent.achat(message, on_persist=on_persist):
            match event.type:
                case "reasoning":
                    self.reasoning(event.data.get("delta", ""))
                case "reasoning_done":
                    duration_ms = event.data.get("duration_ms")
                    if isinstance(duration_ms, int):
                        self._thinking_duration_ms = duration_ms
                case "text":
                    self.text(event.data.get("delta", ""))
                case "tool_start":
                    tool_call = event.data.get("tool_call") or {}
                    self.tool_start(tool_call.get("name", ""), tool_call.get("input") or {})
                case "tool_output":
                    self.tool_output(event.data.get("output", ""))
                case "tool_done":
                    output = str(event.data.get("output") or "")
                    is_error = bool(event.data.get("is_error"))
                    raw_meta = event.data.get("metadata")
                    metadata = raw_meta if isinstance(raw_meta, dict) else None
                    self.tool_done(output, is_error=is_error, metadata=metadata)
                    if is_error:
                        exit_code = 1
                case "usage":
                    self._stats = dict(event.data)
                case "compact":
                    self.compact()
                case "error":
                    exit_code = 1
                    self.error(event.data.get("message", ""))
                case _:
                    pass

        self.finish()
        return exit_code

    def reasoning(self, chunk: str) -> None:
        """Handle one streamed reasoning chunk from the agent."""

        if self._thinking_start_time is None:
            self._thinking_start_time = time.monotonic()
        self._reasoning.append(chunk)
        self._ensure_live()
        self._update()

    def text(self, chunk: str) -> None:
        """Handle one streamed assistant text chunk."""

        self._finalize_reasoning_phase()
        if not self._text_started and self._had_prior_output:
            self._console.print()
            self._text_started = True
        self._text.append(chunk)
        self._ensure_live()
        self._update()

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        """Render the start of a tool call."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()

        self._tool_output_count = 0
        self._tool_name = name
        self._tool_args = args
        self._tool_header_printed = False

        if name.lower() == "bash":
            # Bash streams output. Defer the header until the first output line
            # actually arrives so a hook (e.g. permission review) can interject
            # without leaving an orphan header above the prompt.
            return

        # Other tools are fast — show spinner until tool_done.
        self._tool_live = Live(
            self._build_tool_spinner(name, args),
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._tool_live.start()

    def tool_output(self, line: str) -> None:
        """Render one streamed output line from a running tool."""

        if not line:
            return
        if not self._tool_header_printed:
            self._print_tool_header(self._tool_name, self._tool_args)
            self._tool_header_printed = True
        self._tool_output_count += 1
        if self._tool_output_count <= _TOOL_OUTPUT_MAX_LINES:
            text = Text("    ", style=MUTED)
            text.append(line, style=MUTED)
            self._console.print(text)

    def tool_done(
        self,
        output: str,
        *,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Render the final tool result."""

        # A spinner means a buffered (non-streaming) tool; its success carries an
        # inline header suffix. Bash and every error path use a plain header.
        was_buffered = self._tool_live is not None
        self._stop_tool_live()

        suffix = None
        if was_buffered and not is_error:
            suffix = self._format_tool_suffix(self._tool_name, self._tool_args, metadata)
        if not self._tool_header_printed:
            self._print_tool_header(self._tool_name, self._tool_args, suffix=suffix)
            self._tool_header_printed = True

        if is_error and self._tool_output_count == 0:
            first_line = output.split("\n", 1)[0][:100]
            self._console.print(Text(f"    {first_line}", style=ERROR))
        else:
            truncated = self._tool_output_count - _TOOL_OUTPUT_MAX_LINES
            if truncated > 0:
                self._console.print(Text(f"    +{truncated} lines", style=MUTED))

        self._had_prior_output = True

    def _print_tool_header(
        self,
        name: str,
        args: dict[str, Any],
        *,
        suffix: Text | None = None,
    ) -> None:
        """Print the ``⏺ Name  preview  [suffix]`` tool header line."""

        preview = _tool_preview(name, args)

        text = Text()
        text.append(f"{TOOL_MARKER} ", style=SUCCESS)
        text.append(name.capitalize(), style=TOOL_NAME)
        if preview:
            text.append(f"  {preview}", style=MUTED)
        if suffix:
            text.append("  ")
            text.append_text(suffix)
        self._console.print(text)

    def _build_tool_spinner(self, name: str, args: dict[str, Any]) -> Spinner:
        """Build a transient spinner shown while a buffered tool runs."""

        preview = _tool_preview(name, args)
        label = Text()
        label.append(f" {name.capitalize()}", style=TOOL_NAME)
        if preview:
            label.append(f"  {preview}", style=MUTED)
        return Spinner("dots", text=label, style="dim")

    def _stop_tool_live(self) -> None:
        if self._tool_live is not None:
            self._clear_live(self._tool_live)
            self._tool_live = None

    @staticmethod
    def _format_tool_suffix(
        name: str,
        args: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> Text | None:
        """Build the inline suffix shown after the tool preview on success.

        Edit stats read from backend metadata so TUI and web display identical
        ``+N −M`` counts.
        """

        parts = Text()
        lower = name.lower()

        if lower == "edit":
            added = (metadata or {}).get("added_lines")
            removed = (metadata or {}).get("removed_lines")
            if isinstance(added, int) and isinstance(removed, int):
                parts.append(f"+{added}", style="green")
                parts.append(f" −{removed}", style="red")
        elif lower == "read":
            offset = args.get("offset")
            limit = args.get("limit")
            if isinstance(offset, int) and isinstance(limit, int):
                parts.append(f":{offset}-{offset + limit}", style=MUTED)
            elif isinstance(offset, int):
                parts.append(f":{offset}", style=MUTED)
            elif isinstance(limit, int):
                parts.append(f":1-{limit}", style=MUTED)
        elif lower == "write":
            content = args.get("content")
            if isinstance(content, str):
                lines = content.count("\n") + 1
                parts.append(f"({lines} lines)", style=MUTED)

        return parts if parts.plain else None

    def compact(self) -> None:
        """Render an inline ``compacted`` divider during streaming."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()
        self._console.print(_compact_marker_text(self._console.size.width))

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

    def prepare_interaction(self) -> None:
        """Pause live rendering before handing the terminal to prompt-toolkit."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()

    def finish(self) -> None:
        """Flush the current turn and print the post-turn stats line."""

        self._finalize_reasoning_phase()
        self._reset_stream_state()

        total_tokens = self._stats.get("total_tokens")
        model = self._stats.get("model")
        if total_tokens and model:
            parts = [f"{total_tokens:,} tokens"]
            if context_window := self._stats.get("context_window"):
                parts.append(f"{round(total_tokens * 100 / context_window)}%")
            self._console.print(Text(f"  {model}  {' · '.join(parts)}", style=STATS))

    # -- Internal helpers ----------------------------------------------------

    def _finalize_reasoning_phase(self) -> None:
        """Stop the thinking spinner and print a one-line summary, if any."""

        if self._thinking_collapsed or not self._reasoning:
            return
        self._thinking_collapsed = True

        if self._live is not None:
            self._clear_live(self._live)
            self._live = None

        self._console.print(Text(f"{THINKING_SYMBOL} thought{self._thinking_duration_label()}", style=THINKING))
        self._had_prior_output = True
        self._reasoning.clear()

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
            if self._text:
                self._live.stop()
            else:
                self._clear_live(self._live)
            self._live = None
        self._stop_tool_live()
        self._reasoning.clear()
        self._text.clear()
        self._thinking_collapsed = False
        self._thinking_start_time = None
        self._thinking_duration_ms = None

    def _thinking_duration_label(self) -> str:
        duration_ms = self._thinking_duration_ms
        if duration_ms is None and self._thinking_start_time is not None:
            duration_ms = int((time.monotonic() - self._thinking_start_time) * 1000)
        return f" · {duration_ms / 1000:.1f}s" if duration_ms is not None else ""

    @staticmethod
    def _clear_live(live: Live) -> None:
        live.transient = True
        live.update(Text(""))
        live.stop()

    def _build_live_renderable(self) -> Spinner | _LeftMarkdown:
        """Build the Rich renderable used while a reply is streaming."""

        # No content yet: plain spinner
        if not self._reasoning and not self._text:
            return Spinner("dots", style="dim")

        # Thinking in progress: show rolling preview of reasoning content.
        # Join only the tail to avoid O(full_length) work on every frame.
        if self._reasoning and not self._text:
            tail = "".join(self._reasoning[-30:])
            content = " ".join(tail.split())
            if content:
                preview = content[-80:].strip()
                if len(self._reasoning) > 30 or len(content) > 80:
                    preview = "…" + preview
                return Spinner("dots", text=Text(f" {preview}", style=THINKING), style="dim")
            return Spinner("dots", text=Text(" thinking…", style=THINKING), style="dim")

        # Text streaming: render as markdown (thinking already collapsed)
        if self._text:
            return _LeftMarkdown("".join(self._text), code_theme=CODE_THEME)

        return Spinner("dots", style="dim")
