"""Renderables for the terminal CLI and the per-turn event renderer."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import aclosing
from datetime import datetime
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from mycode.agent import Agent, PersistCallback
from mycode.messages import ConversationMessage, flatten_message_text
from mycode_cli.runtime import sum_known_costs

from .markdown import Chunk, MarkdownBlock, MarkdownStream
from .terminal import Terminal
from .theme import (
    ACCENT,
    ERROR,
    ERROR_MARKER,
    MARKDOWN_THEME,
    MUTED,
    PROMPT_CHAR,
    PROVIDER,
    STATS,
    SUCCESS,
    THINKING,
    THINKING_SYMBOL,
    TOOL_MARKER,
    TOOL_NAME,
    WARNING,
)

# Console for output outside the interactive application (errors, session list).
console = Console(highlight=False, theme=MARKDOWN_THEME)

_TOOL_OUTPUT_MAX_LINES = 5
# Delay that coalesces streamed text deltas into one commit and redraw.
_TEXT_TICK_SECONDS = 0.05

# Maps built-in tool names to the argument key most useful as a one-line preview.
_TOOL_PREVIEW_KEY: dict[str, str] = {
    "read": "path",
    "write": "path",
    "edit": "path",
    "bash": "command",
    "webfetch": "url",
    "websearch": "query",
}


# -- Static renderables ----------------------------------------------------------


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


def compact_marker(width: int) -> Text:
    """Build the inline ``compacted`` divider used in stream and history views."""

    label = " compacted "
    bar = max(1, (max(width, len(label) + 4) - len(label)) // 2)
    return Text(f"{'─' * bar}{label}{'─' * bar}", style=MUTED)


def user_echo(text: str) -> Text:
    """Render submitted user text as ``❯ text`` with continuation lines indented."""

    lines = text.splitlines() or [""]
    echo = Text()
    echo.append(f"{PROMPT_CHAR} ", style=ACCENT)
    echo.append(lines[0])
    for line in lines[1:]:
        echo.append(f"\n  {line}")
    return echo


def header_lines(
    *,
    provider: str,
    model: str,
    session: dict[str, Any],
    mode: str,
    message_count: int,
    reasoning_effort: str | None = None,
) -> list[Text]:
    """Build the session header shown above the interactive chat."""

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
    lines = [line]

    if mode == "resumed":
        meta = Text()
        meta.append("resumed", style=WARNING)
        if title and title != "New chat":
            meta.append(" · ", style=MUTED)
            meta.append(title, style=MUTED)
        if message_count:
            meta.append(" · ", style=MUTED)
            meta.append(f"{message_count} msgs", style=MUTED)
        lines.append(meta)
    return lines


def _tool_header(name: str, args: dict[str, Any], *, suffix: Text | None = None) -> Text:
    """Build the ``⏺ Name  preview  [suffix]`` tool header line."""

    preview = _tool_preview(name, args)
    text = Text()
    text.append(f"{TOOL_MARKER} ", style=SUCCESS)
    text.append(name.capitalize(), style=TOOL_NAME)
    if preview:
        text.append(f"  {preview}", style=MUTED)
    if suffix:
        text.append("  ")
        text.append_text(suffix)
    return text


def _history_turns(messages: list[ConversationMessage], *, limit: int = 3) -> list[list[tuple[str, Any]]]:
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

    return turns[-limit:]


def history_preview(messages: list[ConversationMessage], *, width: int) -> list[RenderableType]:
    """Build the recent-turns preview printed for resumed sessions; empty when there is nothing to show."""

    turns = _history_turns(messages)
    if not turns:
        return []

    lines: list[RenderableType] = [Text("recent", style=MUTED)]
    for turn in turns:
        lines.append(Text())
        for kind, content in turn:
            if kind == "user":
                lines.append(user_echo(str(content)))
            elif kind == "text":
                lines.append(MarkdownBlock(str(content)))
            elif kind == "compact":
                lines.append(compact_marker(width))
            else:
                name, args = content
                lines.append(_tool_header(name, args if isinstance(args, dict) else {}))
    return lines


def _shorten(value: str, *, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def session_list_table(
    sessions: list[dict[str, Any]],
    *,
    include_cwd: bool = False,
    heading: str = "sessions",
) -> list[RenderableType]:
    """Build the saved-session listing: a heading and a compact table."""

    if not sessions:
        return [Text("no sessions found", style=MUTED)]

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
        timestamp = format_local_timestamp(str(session.get("updated_at") or ""), "%Y-%m-%d %H:%M") or "-"
        row: list[RenderableType] = [
            Text(str(index), style=MUTED),
            Text(session_id[:12], style=MUTED),
            Text(timestamp, style=MUTED),
            Text(_shorten(str(session.get("title") or "New chat"), limit=title_limit)),
        ]
        if include_cwd:
            row.append(Text(_shorten(str(session.get("cwd") or ""), limit=cwd_limit), style=MUTED))
        table.add_row(*row)

    return [Text(f"{heading} ({len(sessions)})", style=MUTED), Text(), table]


def _format_cost(cost: float) -> str:
    """Format a USD amount for the stats line; keep sub-cent values readable."""

    return f"${cost:.4f}" if 0 < cost < 0.01 else f"${cost:.2f}"


def _tool_suffix(name: str, args: dict[str, Any], metadata: dict[str, Any] | None) -> Text | None:
    """Build the inline suffix shown after a buffered tool's preview on success.

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


# -- Turn renderer ---------------------------------------------------------------


class TurnRenderer:
    """Render one assistant turn from agent events: reasoning, markdown text, tools, and stats.

    Finished output is printed to the terminal scrollback; the unfinished part
    (spinner or the current markdown block) lives in the terminal tail.
    """

    def __init__(
        self,
        terminal: Terminal,
        *,
        model: str,
        context_window: int | None,
        session_cost_base: float | None = None,
    ) -> None:
        self._terminal = terminal
        self._model = model
        self._context_window = context_window
        self._session_cost_base = session_cost_base
        # One spinner for the whole turn keeps its animation continuous across phases.
        self._spinner = Spinner("dots", style="dim")
        # Whether anything was printed this turn; blocks after the first get a blank line before them.
        self._printed = False
        # Reasoning phase
        self._reasoning: deque[str] = deque(maxlen=30)
        self._reasoning_count = 0
        self._thinking_start_time: float | None = None
        self._thinking_duration_ms: int | None = None
        # Text phase
        self._stream = MarkdownStream()
        self._text_active = False
        self._tick: asyncio.Task[None] | None = None
        # Tool phase
        self._tool_name = ""
        self._tool_args: dict[str, Any] = {}
        self._tool_header_printed = False
        self._tool_line = ""
        self._tool_line_count = 0
        # Stats reported by the agent's `usage` event for the latest request.
        self._stats: dict[str, Any] = {}

    async def render(
        self,
        agent: Agent,
        message: str | ConversationMessage,
        *,
        on_persist: PersistCallback | None = None,
    ) -> int:
        """Stream one assistant turn to the terminal and return its exit code."""

        exit_code = 0
        self._stats = {}
        self._show_spinner(Text())
        try:
            async with aclosing(agent.achat(message, on_persist=on_persist)) as stream:
                async for event in stream:
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
                        case "retry":
                            self.retry(
                                attempt=event.data.get("attempt"),
                                max_attempts=event.data.get("max_attempts"),
                                reason=str(event.data.get("reason") or ""),
                            )
                        case "usage":
                            self._stats = dict(event.data)
                        case "compact":
                            self.compact()
                        case "cancelled":
                            self.cancel()
                            return 0
                        case "error":
                            exit_code = 1
                            self.error(event.data.get("message", ""))
                        case _:
                            pass
            self.finish()
            return exit_code
        finally:
            self._cancel_tick()
            self._terminal.set_tail(None)

    def reasoning(self, delta: str) -> None:
        """Handle one streamed reasoning delta: a spinner with a rolling preview."""

        self._end_text()
        if self._thinking_start_time is None:
            self._thinking_start_time = time.monotonic()
        self._reasoning.append(delta)
        self._reasoning_count += 1

        content = " ".join("".join(self._reasoning).split())
        if not content:
            self._show_spinner(Text(" thinking…", style=THINKING))
            return
        preview = content[-80:].strip()
        if self._reasoning_count > 30 or len(content) > 80:
            preview = "…" + preview
        self._show_spinner(Text(f" {preview}", style=THINKING))

    def text(self, delta: str) -> None:
        """Handle one streamed text delta; commits happen on a short coalescing tick."""

        self._end_reasoning()
        self._text_active = True
        self._stream.feed(delta)
        if self._tick is None:
            self._tick = asyncio.get_running_loop().create_task(self._text_tick())

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        """Render the start of a tool call."""

        self._end_phase()
        self._tool_name = name
        self._tool_args = args
        self._tool_header_printed = False
        self._tool_line = ""
        self._tool_line_count = 0

        # The header is printed with the first output (bash) or the result (other
        # tools), so a permission review never leaves an orphan header above it.
        label = Text()
        label.append(f" {name.capitalize()}", style=TOOL_NAME)
        preview = _tool_preview(name, args)
        if preview:
            label.append(f"  {preview}", style=MUTED)
        self._show_spinner(label)

    def tool_output(self, delta: str) -> None:
        """Append streamed tool output; only complete lines are printed."""

        if not delta:
            return
        if not self._tool_header_printed:
            self._print(_tool_header(self._tool_name, self._tool_args))
            self._tool_header_printed = True
            self._show_spinner(Text())

        *complete, self._tool_line = (self._tool_line + delta).split("\n")
        for line in complete:
            self._print_tool_line(line)

    def tool_done(
        self,
        output: str,
        *,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Render the final tool result."""

        # Bash streams; other tools are buffered and get an inline header suffix on success.
        suffix = None
        if self._tool_name.lower() != "bash" and not is_error:
            suffix = _tool_suffix(self._tool_name, self._tool_args, metadata)
        if not self._tool_header_printed:
            self._print(_tool_header(self._tool_name, self._tool_args, suffix=suffix))
            self._tool_header_printed = True

        if self._tool_line:
            self._print_tool_line(self._tool_line)
            self._tool_line = ""

        hidden_lines = self._tool_line_count - _TOOL_OUTPUT_MAX_LINES
        if hidden_lines > 0:
            self._print(Text(f"    +{hidden_lines} lines", style=MUTED))

        result_lines = output.splitlines()
        status_prefixes = ("error:", "[Output truncated:", "[Command timed out", "[exit code:")
        status_lines = [line for line in result_lines if line.startswith(status_prefixes)]
        if is_error and not status_lines and result_lines:
            status_lines = [result_lines[-1]]
        style = ERROR if is_error else MUTED
        for line in status_lines[-2:]:
            self._print(Text(f"    {line[:500]}", style=style))

        # Waiting for the next model response.
        self._show_spinner(Text())

    def retry(self, *, attempt: object, max_attempts: object, reason: str) -> None:
        """Show the retry status in the tail until the next event replaces it."""

        self._show_spinner(Text(f" retry {attempt}/{max_attempts} · {reason}", style=MUTED))

    def compact(self) -> None:
        """Render an inline ``compacted`` divider during streaming."""

        self._end_phase()
        self._print(compact_marker(self._terminal.width))
        self._show_spinner(Text())

    def error(self, message: str) -> None:
        """Render a terminal-visible error message for the current turn."""

        self._end_phase()
        text = Text(f"{ERROR_MARKER} ", style=ERROR)
        text.append(message, style=ERROR)
        self._print(text)

    def cancel(self) -> None:
        """Render a cancellation marker."""

        self._end_phase()
        self._print(Text("cancelled", style=MUTED))

    def finish(self) -> None:
        """Flush the current turn and print the post-turn stats line."""

        self._end_phase()
        context_tokens = self._stats.get("context_tokens")
        parts: list[str] = []
        if context_tokens:
            usage_text = f"{context_tokens:,} tokens"
            if self._context_window:
                usage_text += f" ({round(context_tokens * 100 / self._context_window)}%)"
            parts.append(usage_text)
        turn_cost = self._stats.get("turn_cost")
        turn_total = turn_cost.get("total") if isinstance(turn_cost, dict) else None
        session_cost = sum_known_costs(self._session_cost_base, turn_total)
        if session_cost is not None:
            parts.append(_format_cost(session_cost))
        if parts:
            self._print(Text(f"  {self._model}  {' · '.join(parts)}", style=STATS))

    # -- Internal helpers ----------------------------------------------------

    def _print(self, renderable: RenderableType) -> None:
        self._terminal.print(renderable)
        self._printed = True

    def _show_spinner(self, text: Text) -> None:
        self._spinner.text = text
        self._terminal.set_tail(self._spinner)

    def _print_tool_line(self, line: str) -> None:
        self._tool_line_count += 1
        if self._tool_line_count <= _TOOL_OUTPUT_MAX_LINES:
            self._print(Text(f"    {line}", style=MUTED))

    def _end_phase(self) -> None:
        self._end_reasoning()
        self._end_text()

    def _end_reasoning(self) -> None:
        """Collapse an active reasoning phase into a one-line summary."""

        if not self._reasoning_count:
            return
        duration_ms = self._thinking_duration_ms
        if duration_ms is None and self._thinking_start_time is not None:
            duration_ms = int((time.monotonic() - self._thinking_start_time) * 1000)
        label = f" · {duration_ms / 1000:.1f}s" if duration_ms is not None else ""
        self._print(Text(f"{THINKING_SYMBOL} thought{label}", style=THINKING))
        self._reasoning.clear()
        self._reasoning_count = 0
        self._thinking_start_time = None
        self._thinking_duration_ms = None

    def _end_text(self) -> None:
        """Print whatever is left of an active text phase."""

        if not self._text_active:
            return
        self._text_active = False
        self._cancel_tick()
        self._print_chunks(self._stream.flush())

    def _cancel_tick(self) -> None:
        if self._tick is not None:
            self._tick.cancel()
            self._tick = None

    async def _text_tick(self) -> None:
        """Commit finished markdown to scrollback and redraw the unfinished tail."""

        await asyncio.sleep(_TEXT_TICK_SECONDS)
        self._tick = None
        # One line of the tail may go to the blank line that separates it from earlier output.
        chunks = self._stream.commit(width=self._terminal.width, height=self._terminal.tail_height - 1)
        self._print_chunks(chunks)
        tail = self._stream.tail
        if not tail:
            self._show_spinner(Text())
        elif self._printed and not self._stream.tail_continues:
            self._terminal.set_tail(Group(Text(), MarkdownBlock(tail)))
        else:
            self._terminal.set_tail(MarkdownBlock(tail))

    def _print_chunks(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            if self._printed and not chunk.continues:
                self._terminal.print()
            self._print(MarkdownBlock(chunk.source))
