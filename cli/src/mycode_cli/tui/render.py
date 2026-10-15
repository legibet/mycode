"""Renderables for the terminal CLI and the per-turn event renderer."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from mycode.agent import Agent, PersistCallback
from mycode.events import (
    CancelledEvent,
    CompactEvent,
    ErrorEvent,
    ReasoningDoneEvent,
    ReasoningEvent,
    RetryEvent,
    TextEvent,
    ToolDoneEvent,
    ToolOutputEvent,
    ToolStartEvent,
    UsageEvent,
    UserMessageEvent,
)
from mycode.messages import ConversationMessage, TextBlock
from mycode_cli.background import job_id
from mycode_cli.sessions import DEFAULT_SESSION_TITLE, SessionInfo, SessionTotals, display_text

from .markdown import Chunk, MarkdownBlock, MarkdownStream
from .terminal import Terminal
from .theme import (
    ACCENT,
    ERROR,
    ERROR_MARKER,
    MARKDOWN_THEME,
    MUTED,
    PROMPT_CHAR,
    SUCCESS,
    THINKING,
    THINKING_SYMBOL,
    TOOL_MARKER,
    TOOL_NAME,
    WARNING,
)

# Console for output outside the interactive application (errors, session list).
console = Console(highlight=False, markup=False, theme=MARKDOWN_THEME)

_TOOL_OUTPUT_MAX_LINES = 5
# The spinner frame and the space rich puts after it.
_SPINNER_CELLS = 2
# Reasoning text kept for the rolling one-line preview.
_REASONING_KEEP_CHARS = 1000
# Delay that coalesces streamed text deltas into one commit and redraw.
_TEXT_TICK_SECONDS = 0.05

# Built-in tools: display name and the argument shown as the one-line preview.
_BUILTIN_TOOLS: dict[str, tuple[str, str]] = {
    "read": ("Read", "path"),
    "write": ("Write", "path"),
    "edit": ("Edit", "path"),
    "bash": ("Bash", "command"),
    "webfetch": ("WebFetch", "url"),
    "websearch": ("WebSearch", "query"),
}

# Notices bash appends to its output as separate paragraphs (docs/tools.md).
_BASH_NOTICES = ("[Output truncated:", "[Command timed out", "[exit code:", "error: cancelled")

# CSI, OSC, and two-character escape sequences in tool output.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")


# -- Static renderables ----------------------------------------------------------


def shorten(value: str, width: int) -> str:
    """Collapse whitespace to single spaces and cut to ``width`` terminal cells with an ellipsis."""

    text = Text(" ".join(value.split()))
    text.truncate(width, overflow="ellipsis")
    return text.plain


def _last_cells(value: str, width: int) -> str:
    """Return the end of ``value`` that fits ``width`` terminal cells, with a leading ellipsis when cut."""

    if cell_len(value) <= width:
        return value
    start, cells = len(value), 1
    while start and cells + cell_len(value[start - 1]) <= width:
        start -= 1
        cells += cell_len(value[start])
    return "…" + value[start:]


def _display_line(line: str) -> str:
    """Return a tool output line as a terminal shows it: escapes removed, only the text after the last ``\\r``."""

    return _ANSI_ESCAPE.sub("", line).rstrip("\r").rsplit("\r", 1)[-1]


def _result_status(output: str, *, is_error: bool) -> list[str]:
    """Return the result lines shown under a finished tool: bash notices, or the error of a failed tool.

    Lines of the command's own output are never picked; its last lines are shown already.
    """

    paragraphs = output.split("\n\n")
    notices: list[str] = []
    while paragraphs and paragraphs[-1].startswith(_BASH_NOTICES):
        notices.insert(0, paragraphs.pop())
    if notices or not is_error:
        return notices
    # Without notices, a failed tool's output is its error message.
    return output.splitlines()[:1]


def error_line(message: str) -> Text:
    """Build the ``✕ message`` error line."""

    return Text(f"{ERROR_MARKER} {message}", style=ERROR)


def format_local_timestamp(value: str, display_format: str) -> str:
    """Format an ISO timestamp with a simple local fallback."""

    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp.astimezone().strftime(display_format)
    except ValueError:
        return value[:16].replace("T", " ")


def compact_marker() -> Text:
    """Build the inline ``compacted`` divider used in stream and history views."""

    return Text("── compacted ──", style=MUTED)


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
    reasoning_effort: str | None,
    session_id: str,
    resumed: SessionInfo | None,
    message_count: int,
) -> list[Text]:
    """Build the session header shown above the interactive chat; ``resumed`` is the resumed session's entry."""

    line = Text()
    line.append("mycode", style=ACCENT)
    line.append(" · ", style=MUTED)
    line.append(provider)
    line.append(" / ", style=MUTED)
    line.append(model)
    if reasoning_effort:
        line.append(" · ", style=MUTED)
        line.append(reasoning_effort, style=MUTED)
    line.append(" · ", style=MUTED)
    line.append(session_id[:8], style=MUTED)
    lines = [line]

    if resumed is not None:
        meta = Text()
        meta.append("resumed", style=MUTED)
        if resumed["title"] != DEFAULT_SESSION_TITLE:
            meta.append(" · ", style=MUTED)
            meta.append(resumed["title"], style=MUTED)
        if message_count:
            meta.append(" · ", style=MUTED)
            meta.append(f"{message_count} msgs", style=MUTED)
        lines.append(meta)
    return lines


def tool_label(name: str) -> str:
    """Return the display name of a tool."""

    return _BUILTIN_TOOLS[name][0] if name in _BUILTIN_TOOLS else name


def _tool_title(name: str, args: dict[str, Any], width: int, *, suffix: Text | None = None) -> Text:
    """Build ``Name  preview  [suffix]`` on one line of ``width`` cells; only the preview is shortened.

    The preview is the tool's main argument, or the first one for tools that are not built in.
    """

    key = _BUILTIN_TOOLS[name][1] if name in _BUILTIN_TOOLS else ""
    raw = args.get(key) if key else next(iter(args.values()), "")
    preview = " ".join(str(raw or "").split())
    text = Text(tool_label(name), style=TOOL_NAME)
    tail = Text("  ").append_text(suffix) if suffix else Text()
    room = width - text.cell_len - tail.cell_len - 2
    if room > 1 and preview:
        # A path keeps its end, where the file name is.
        text.append(f"  {_last_cells(preview, room) if key == 'path' else shorten(preview, room)}", style=MUTED)
    return text.append_text(tail)


def _tool_header(
    name: str, args: dict[str, Any], width: int, *, failed: bool, metadata: dict[str, Any] | None = None
) -> Text:
    """Build the ``● Name  preview  [suffix]`` line of a finished tool call; the suffix shows on success."""

    marker = Text(f"{TOOL_MARKER} ", style=ERROR if failed else SUCCESS, no_wrap=True, overflow="ellipsis")
    suffix = None if failed else _tool_suffix(name, args, metadata)
    return marker.append_text(_tool_title(name, args, width - marker.cell_len, suffix=suffix))


def _job_blocks(message: ConversationMessage) -> list[TextBlock]:
    """Return the background job notification blocks of a user message."""

    return [block for block in message["content"] if block["type"] == "text" and job_id(block)]


def job_lines(block: TextBlock, width: int) -> list[Text]:
    """Render a finished background command: ``● Background finished  label  exit N``, then the output tail.

    The header names the event rather than the tool, so it does not read as a second ``Bash`` call.
    """

    job = block.get("meta", {}).get("job") or {}
    exit_code = job.get("exit_code") or 0
    marker = Text(f"{TOOL_MARKER} ", style=ERROR if exit_code else SUCCESS, no_wrap=True, overflow="ellipsis")
    title = Text("Background finished", style=TOOL_NAME)
    tail = Text(f"  exit {exit_code}", style=ERROR if exit_code else MUTED)
    room = width - marker.cell_len - title.cell_len - tail.cell_len - 2
    label = str(job.get("label") or "")
    if room > 1 and label:
        title.append(f"  {shorten(label, room)}", style=MUTED)
    lines = [marker.append_text(title).append_text(tail)]

    # The text is the header, a blank line, the output tail, then any bash notices.
    _, _, body = block["text"].partition("\n\n")
    paragraphs = body.split("\n\n")
    notices: list[str] = []
    while paragraphs and paragraphs[-1].startswith(_BASH_NOTICES):
        notices.insert(0, paragraphs.pop())
    output = [line for line in "\n\n".join(paragraphs).split("\n") if line.strip() and line != "(empty)"]
    if len(output) > _TOOL_OUTPUT_MAX_LINES:
        lines.append(Text(f"  … +{len(output) - _TOOL_OUTPUT_MAX_LINES} lines", style=MUTED))
        output = output[-_TOOL_OUTPUT_MAX_LINES:]
    lines.extend(Text(f"  {_display_line(line)[:500]}", style=MUTED) for line in output)
    lines.extend(Text(f"  {_display_line(notice)[:500]}", style=MUTED) for notice in notices)
    return lines


def _history_turns(messages: list[ConversationMessage], *, limit: int = 3) -> list[list[tuple[str, Any]]]:
    """Return the last few readable conversation turns for resumed sessions."""

    turns: list[list[tuple[str, Any]]] = []
    results = {
        block["tool_use_id"]: block
        for message in messages
        if message["role"] == "user"
        for block in message["content"]
        if block["type"] == "tool_result"
    }

    for message in messages:
        role = message["role"]

        if role == "compact":
            turns.append([("compact", None)])
            continue

        if role == "user":
            # display_text keeps payload blocks out of the readable history
            # preview; background results print as tools.
            parts: list[tuple[str, Any]] = [("job", block) for block in _job_blocks(message)]
            if text := display_text(message):
                parts.append(("user", text))
            if parts:
                turns.append(parts)
            continue

        parts: list[tuple[str, Any]] = []
        for block in message["content"]:
            if block["type"] == "text":
                if text := block["text"].strip():
                    parts.append(("text", text))
            elif block["type"] == "tool_use":
                result = results.get(block["id"]) or {}
                parts.append(("tool", (block["name"] or "tool", block["input"], result)))

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
            elif kind == "job":
                lines.extend(job_lines(content, width))
            elif kind == "text":
                lines.append(MarkdownBlock(str(content)))
            elif kind == "compact":
                lines.append(compact_marker())
            else:
                name, args, result = content
                lines.append(
                    _tool_header(
                        name, args, width, failed=bool(result.get("is_error")), metadata=result.get("metadata")
                    )
                )
    return lines


def session_list_table(
    sessions: list[SessionInfo],
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
        row: list[RenderableType] = [
            Text(str(index), style=MUTED),
            Text(session["id"][:12], style=MUTED),
            Text(format_local_timestamp(session["updated_at"], "%Y-%m-%d %H:%M"), style=MUTED),
            Text(shorten(session["title"], title_limit)),
        ]
        if include_cwd:
            row.append(Text(shorten(session["cwd"], cwd_limit), style=MUTED))
        table.add_row(*row)

    return [Text(f"{heading} ({len(sessions)})", style=MUTED), Text(), table]


def _format_cost(cost: float) -> str:
    """Format a USD amount for the stats line; keep sub-cent values readable."""

    return f"${cost:.4f}" if 0 < cost < 0.01 else f"${cost:.2f}"


def _tool_suffix(name: str, args: dict[str, Any], metadata: dict[str, Any] | None) -> Text | None:
    """Build the inline suffix shown after a successful tool's preview.

    Edit stats read from backend metadata so TUI and web display identical
    ``+N −M`` counts.
    """

    parts = Text()

    if name == "edit":
        added = (metadata or {}).get("added_lines")
        removed = (metadata or {}).get("removed_lines")
        if isinstance(added, int) and isinstance(removed, int):
            parts.append(f"+{added}", style=SUCCESS)
            parts.append(f" −{removed}", style=ERROR)
    elif name == "read":
        offset = args.get("offset")
        limit = args.get("limit")
        if isinstance(offset, int) and isinstance(limit, int):
            parts.append(f":{offset}-{offset + limit}", style=MUTED)
        elif isinstance(offset, int):
            parts.append(f":{offset}", style=MUTED)
        elif isinstance(limit, int):
            parts.append(f":1-{limit}", style=MUTED)
    elif name == "write":
        content = args.get("content")
        if isinstance(content, str):
            lines = content.count("\n") + 1
            parts.append(f"({lines} lines)", style=MUTED)
    elif name == "bash" and (metadata or {}).get("background"):
        parts.append("background", style=MUTED)

    return parts if parts.plain else None


# -- Turn renderer ---------------------------------------------------------------


@dataclass
class _RunningTool:
    """A tool call in the tail: its last complete output lines, the unfinished line, and the complete line count."""

    name: str
    args: dict[str, Any]
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=_TOOL_OUTPUT_MAX_LINES))
    line: str = ""
    line_count: int = 0

    def feed(self, delta: str) -> None:
        *complete, self.line = (self.line + delta).split("\n")
        self.lines.extend(complete)
        self.line_count += len(complete)

    def output(self, rows: int = _TOOL_OUTPUT_MAX_LINES + 1) -> list[Text]:
        """At most ``rows`` rows: the last output lines, after a count of the lines left out."""

        if rows <= 0:
            return []
        lines = [*self.lines, self.line] if self.line else [*self.lines]
        total = self.line_count + bool(self.line)
        keep = min(_TOOL_OUTPUT_MAX_LINES, rows)
        if total > keep:
            # One row goes to the count.
            keep = min(keep, rows - 1)
        shown = lines[-keep:] if keep else []
        hidden = total - len(shown)
        count = [Text(f"  … +{hidden} lines", style=MUTED)] if hidden else []
        return count + [
            Text(f"  {_display_line(line)}", style=MUTED, no_wrap=True, overflow="ellipsis") for line in shown
        ]


class TurnRenderer:
    """Render one assistant turn from agent events: reasoning, markdown text, tools, and stats.

    Finished output is printed to the terminal scrollback; the unfinished part
    (spinner, current markdown block, or running tool) lives in the terminal tail.
    """

    def __init__(
        self,
        terminal: Terminal,
        *,
        model: str,
        context_window: int | None,
        session_base: SessionTotals | None = None,
        on_user_message: Callable[[ConversationMessage], None] | None = None,
    ) -> None:
        self._terminal = terminal
        self._model = model
        self._context_window = context_window
        # Session totals before the current segment; a delivered steer starts a new one.
        self._session_base = session_base or SessionTotals()
        self._on_user_message = on_user_message
        # One spinner for the whole turn keeps its animation continuous across phases.
        self._spinner = Spinner("dots", style=MUTED)
        # Whether anything was printed this turn; blocks after the first get a blank line before them.
        self._printed = False
        # Whether the last printed block is a tool; consecutive tools are not separated.
        self._last_tool = False
        # Reasoning phase
        self._reasoning = ""
        self._thinking_start_time: float | None = None
        self._thinking_duration_ms: int | None = None
        # Text phase
        self._stream = MarkdownStream()
        self._text_active = False
        self._tick: asyncio.Task[None] | None = None
        # Tool phase: the running tools by call id, in call order.
        self._tools: dict[str, _RunningTool] = {}
        # Stats reported by the agent's `usage` event for the latest request.
        self._stats: UsageEvent | None = None
        # Whether the turn ended on a cancel or an error rather than completing.
        self.stopped = False

    async def render(
        self,
        agent: Agent,
        message: str | ConversationMessage,
        *,
        on_persist: PersistCallback | None = None,
    ) -> int:
        """Stream one assistant turn to the terminal and return its exit code."""

        exit_code = 0
        self._stats = None
        self._show_spinner(Text())
        try:
            async with aclosing(agent.achat(message, on_persist=on_persist)) as stream:
                async for event in stream:
                    match event:
                        case ReasoningEvent():
                            self.reasoning(event.delta)
                        case ReasoningDoneEvent():
                            self._thinking_duration_ms = event.duration_ms
                        case TextEvent():
                            self.text(event.delta)
                        case ToolStartEvent():
                            tool_call = event.tool_call
                            self.tool_start(tool_call["id"], tool_call["name"], tool_call["input"])
                        case ToolOutputEvent():
                            self.tool_output(event.tool_use_id, event.output)
                        case ToolDoneEvent():
                            self.tool_done(
                                event.tool_use_id, event.output, is_error=event.is_error, metadata=event.metadata
                            )
                            if event.is_error:
                                exit_code = 1
                        case RetryEvent():
                            self.retry(attempt=event.attempt, max_attempts=event.max_attempts, reason=event.reason)
                        case UsageEvent():
                            self._stats = event
                        case CompactEvent():
                            self.compact()
                        case UserMessageEvent():
                            self.user_message(event.message)
                        case CancelledEvent():
                            self.cancel()
                            return 0
                        case ErrorEvent():
                            exit_code = 1
                            self.error(event.message)
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
        self._reasoning = (self._reasoning + delta)[-_REASONING_KEEP_CHARS:]

        content = " ".join(self._reasoning.split())
        preview = _last_cells(content, self._terminal.width - _SPINNER_CELLS) if content else "thinking…"
        self._show_spinner(Text(preview, style=THINKING))

    def text(self, delta: str) -> None:
        """Handle one streamed text delta; commits happen on a short coalescing tick."""

        self._end_reasoning()
        self._text_active = True
        self._stream.feed(delta)
        if self._tick is None:
            self._tick = asyncio.get_running_loop().create_task(self._text_tick())

    def tool_start(self, tool_id: str, name: str, args: dict[str, Any]) -> None:
        """Show a running tool in the tail; it reaches the scrollback once it finishes."""

        self._end_phase()
        self._tools[tool_id] = _RunningTool(name, args)
        self._show_tools()

    def tool_output(self, tool_id: str, delta: str) -> None:
        """Append streamed tool output to its running tool."""

        self._tools[tool_id].feed(delta)
        self._show_tools()

    def tool_done(
        self,
        tool_id: str,
        output: str,
        *,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Print the finished tool: its header, last output lines, and result status.

        Parallel tools print in the order they finish; the others stay in the tail.
        """

        tool = self._tools.pop(tool_id)
        style = ERROR if is_error else MUTED
        self._print(
            _tool_header(tool.name, tool.args, self._terminal.width, failed=is_error, metadata=metadata),
            *tool.output(),
            *(
                Text(f"  {_display_line(line)[:500]}", style=style)
                for line in _result_status(output, is_error=is_error)
            ),
            joined=self._last_tool,
        )
        self._last_tool = True
        if self._tools:
            self._show_tools()
        else:
            # Waiting for the next model response.
            self._show_spinner(Text())

    def print_block(self, *renderables: RenderableType) -> None:
        """Print a block from outside the event stream, such as a decided tool review."""

        self._print(*renderables)

    def retry(self, *, attempt: int, max_attempts: int, reason: str) -> None:
        """Show the retry status in the tail until the next event replaces it."""

        self._show_spinner(Text(f"retry {attempt}/{max_attempts} · {reason}", style=WARNING))

    def compact(self) -> None:
        """Render an inline ``compacted`` divider during streaming."""

        self._end_phase()
        self._print(compact_marker())
        self._show_spinner(Text())

    def user_message(self, message: ConversationMessage) -> None:
        """Close the segment a delivered steer ends and show the steer; the turn goes on.

        Background results print like finished tools; the user's text, when
        there is any, is echoed with a ``steer`` tag.
        """

        self.finish()
        for block in _job_blocks(message):
            self._print(*job_lines(block, self._terminal.width), joined=self._last_tool)
            self._last_tool = True
        if text := display_text(message):
            echo = user_echo(text)
            echo.append("  steer", style=MUTED)
            self._print(echo)
        if self._stats is not None:
            self._session_base = self._session_base.add(self._stats.turn_usage, self._stats.turn_cost)
            self._stats = None
        if self._on_user_message is not None:
            self._on_user_message(message)
        self._show_spinner(Text())

    def error(self, message: str) -> None:
        """Render a terminal-visible error message for the current turn."""

        self._end_phase()
        self._print(error_line(message))
        self.stopped = True

    def cancel(self) -> None:
        """Render a cancellation marker."""

        self._end_phase()
        self._print(Text("cancelled", style=MUTED))
        self.stopped = True

    def finish(self) -> None:
        """Flush the current segment and print its stats line."""

        self._end_phase()
        context_tokens = self._stats.context_tokens if self._stats else None
        parts: list[str] = []
        if context_tokens:
            usage_text = f"{context_tokens:,} tokens"
            if self._context_window:
                usage_text += f" ({round(context_tokens * 100 / self._context_window)}%)"
            parts.append(usage_text)
        session_cost = self._session_base.add(None, self._stats.turn_cost if self._stats else None).cost
        if session_cost is not None:
            parts.append(_format_cost(session_cost["total"]))
        if parts:
            self._print(Text(" · ".join([self._model, *parts]), style=MUTED))

    # -- Internal helpers ----------------------------------------------------

    def _print(self, *renderables: RenderableType, joined: bool = False) -> None:
        """Print one block, after a blank line unless it is the first or ``joined`` to the previous one."""

        if self._printed and not joined:
            self._terminal.print()
        self._terminal.print(*renderables)
        self._printed = True
        self._last_tool = False

    def _set_tail(self, renderable: RenderableType, *, joined: bool = False) -> None:
        """Show the next block in the tail, spaced as it will be once printed."""

        self._terminal.set_tail(Group(Text(), renderable) if self._printed and not joined else renderable)

    def _show_spinner(self, text: Text) -> None:
        self._spinner.text = text
        self._set_tail(self._spinner)

    def _show_tools(self) -> None:
        """Show the running tools in call order: every title first, then output in the rows left.

        The terminal shows the last lines of a tall tail, so the rows leave room for the blank
        line before it; titles that still do not fit collapse into a count.
        """

        joined = self._last_tool
        rows = self._terminal.tail_height - (0 if joined or not self._printed else 1)
        tools = list(self._tools.values())
        hidden = 0
        if len(tools) > rows:
            hidden = len(tools) - (rows - 1)
            tools = tools[: rows - 1]
        output_rows = rows - len(tools) - bool(hidden)
        renderables: list[RenderableType] = []
        for tool in tools:
            title = _tool_title(tool.name, tool.args, self._terminal.width - _SPINNER_CELLS)
            spinner = Spinner("dots", text=title, style=MUTED)
            # The turn spinner's start time keeps every tool spinner in step with it.
            spinner.start_time = self._spinner.start_time
            output = tool.output(output_rows)
            output_rows -= len(output)
            renderables += [spinner, *output]
        if hidden:
            renderables.append(Text(f"  … +{hidden} running", style=MUTED))
        self._set_tail(Group(*renderables), joined=joined)

    def _end_phase(self) -> None:
        self._end_reasoning()
        self._end_text()

    def _end_reasoning(self) -> None:
        """Collapse an active reasoning phase into a one-line summary."""

        if self._thinking_start_time is None:
            return
        duration_ms = self._thinking_duration_ms
        if duration_ms is None:
            duration_ms = int((time.monotonic() - self._thinking_start_time) * 1000)
        self._print(Text(f"{THINKING_SYMBOL} thought · {duration_ms / 1000:.1f}s", style=THINKING))
        self._reasoning = ""
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
        else:
            self._set_tail(MarkdownBlock(tail), joined=self._stream.tail_continues)

    def _print_chunks(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._print(MarkdownBlock(chunk.source), joined=chunk.continues)
