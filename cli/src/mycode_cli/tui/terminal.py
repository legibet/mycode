"""The terminal owner for the interactive chat.

``Terminal`` runs one persistent, non-fullscreen prompt_toolkit application.
Finished output goes to the terminal scrollback above it; only the bottom area
(tail, chooser, pending input, input, toolbar) is redrawn. Nothing else writes
to the terminal while it runs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from io import StringIO
from typing import Literal, cast, override

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import ConditionalKeyBindings, KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import DisplayMultipleCursors, HighlightSelectionProcessor
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console, RenderableType
from rich.text import Text

from .theme import ACCENT, CHOICE_MARKER, ERROR, MARKDOWN_THEME, MUTED, PROMPT_CHAR, PROMPT_STYLE, WARNING

# Detected once, before prompt_toolkit takes over stdout; None when stdout is not a terminal.
_COLOR_SYSTEM = cast(Literal["standard", "256", "truecolor", "windows"] | None, Console().color_system)

# Redraw rate for spinners while a run is active.
_ANIMATION_INTERVAL = 0.1
# Esc cancels a run but also prefixes Esc+Enter; keep the wait for a following key short.
_ESCAPE_FLUSH_TIMEOUT = 0.1
_KEY_SEQUENCE_TIMEOUT = 0.4
# Lines the input area reserves while the completion menu is open.
_MENU_RESERVED_LINES = 8
# OSC 8 hyperlink sequences from rich; prompt_toolkit's ANSI parser does not understand them.
_HYPERLINK = re.compile("\x1b]8;.*?\x1b\\\\")


def _render(renderables: Iterable[RenderableType], width: int, *, end: str = "\n") -> str:
    """Render rich renderables to ANSI text, each starting on its own line."""

    file = StringIO()
    console = Console(
        file=file,
        force_terminal=True,
        color_system=_COLOR_SYSTEM,
        width=width,
        highlight=False,
        markup=False,
        theme=MARKDOWN_THEME,
    )
    for renderable in renderables:
        if isinstance(renderable, Text):
            # Console.print joins Text into a new Text, which drops its no_wrap and overflow.
            console.print(renderable, end=end, no_wrap=renderable.no_wrap, overflow=renderable.overflow)
        else:
            console.print(renderable, end=end)
    return file.getvalue()


@dataclass(frozen=True)
class _Row:
    label: Text
    heading: bool
    # Lowercase text the filter matches: the row's label after its heading.
    search: str


@dataclass
class _Choice:
    rows: list[_Row]
    # The focused row, or None while the filter matches nothing.
    index: int | None
    future: asyncio.Future[int | None]
    query: str = ""

    def visible(self) -> list[int]:
        """Rows matching the filter; a heading shows while any of its rows does."""

        query = self.query.lower()
        visible: list[int] = []
        heading: int | None = None
        for index, row in enumerate(self.rows):
            if row.heading:
                heading = index
            elif query in row.search:
                if heading is not None and heading not in visible:
                    visible.append(heading)
                visible.append(index)
        return visible

    def selectable(self) -> list[int]:
        return [index for index in self.visible() if not self.rows[index].heading]

    def set_query(self, query: str) -> None:
        self.query = query
        selectable = self.selectable()
        if self.index not in selectable:
            self.index = selectable[0] if selectable else None


class _LogHandler(logging.Handler):
    """Route log records through the terminal; written to stderr they would corrupt the frame."""

    def __init__(self, terminal: Terminal, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._terminal = terminal
        self._loop = loop

    @override
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message = f"{message}: {record.exc_info[1]!r}"
        if record.levelno >= logging.ERROR:
            style = ERROR
        elif record.levelno >= logging.WARNING:
            style = WARNING
        else:
            style = MUTED
        # Sync tools run in worker threads, so the print must be handed to the loop thread.
        self._loop.call_soon_threadsafe(
            self._terminal.print, Text(f"{record.levelname.lower()}: {message}", style=style)
        )


class Terminal:
    """Own the terminal while the chat runs: input, tail area, chooser, toolbar, and scrollback output."""

    def __init__(
        self,
        *,
        history_path: str,
        completer: Completer,
        key_bindings: KeyBindings | None = None,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self.on_cancel: Callable[[], None] | None = None
        # While busy, submitted text goes here with whether Ctrl+Q queued it; it returns whether it took the text.
        self.on_submit: Callable[[str, bool], bool] | None = None
        self.on_take_back: Callable[[], None] | None = None
        self._busy = False
        self._animation: asyncio.Task[None] | None = None
        # Inputs submitted while idle; None marks Ctrl+D.
        self._inputs: asyncio.Queue[str | None] = asyncio.Queue()
        # One line per input a run holds for later, shown above the input.
        self._pending_input: list[Text] = []
        self._tail: RenderableType | None = None
        # Rows the tail area keeps during a run, so the input never moves back up mid-run.
        self._tail_rows = 0
        # A tail replacement waiting for queued prints, wrapped so that None can be queued too.
        self._next_tail: tuple[RenderableType | None] | None = None
        self._pending: list[str] = []
        self._writer: asyncio.Task[None] | None = None
        self._choice: _Choice | None = None
        self._prompt = to_formatted_text(ANSI(_render([Text(f"{PROMPT_CHAR} ", style=ACCENT)], 80, end="")))

        self._buffer = Buffer(
            multiline=True,
            history=FileHistory(history_path),
            completer=completer,
            complete_while_typing=True,
            enable_history_search=False,
            accept_handler=self._accept,
        )
        self._input_window = Window(
            BufferControl(
                self._buffer,
                input_processors=[HighlightSelectionProcessor(), DisplayMultipleCursors()],
                include_default_input_processors=False,
            ),
            height=self._input_height,
            get_line_prefix=self._line_prefix,
            wrap_lines=True,
            dont_extend_height=True,
        )
        self._chooser_window = Window(
            FormattedTextControl(self._chooser_text, focusable=True, show_cursor=False),
            height=lambda: Dimension(max=self.tail_height),
            dont_extend_height=True,
            always_hide_cursor=True,
        )

        busy = Condition(lambda: self._busy)
        choosing = Condition(lambda: self._choice is not None)
        layout = HSplit(
            [
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._tail_text),
                        height=lambda: Dimension(max=self.tail_height),
                        dont_extend_height=True,
                    ),
                    filter=Condition(lambda: self._tail is not None or self._busy) & ~choosing,
                ),
                ConditionalContainer(self._chooser_window, filter=choosing),
                ConditionalContainer(
                    FloatContainer(
                        HSplit(
                            [
                                Window(height=1),
                                ConditionalContainer(
                                    Window(FormattedTextControl(self._pending_text), dont_extend_height=True),
                                    filter=Condition(lambda: bool(self._pending_input)),
                                ),
                                self._input_window,
                            ]
                        ),
                        floats=[
                            Float(
                                xcursor=True,
                                ycursor=True,
                                transparent=True,
                                content=CompletionsMenu(max_height=16, scroll_offset=1),
                            )
                        ],
                    ),
                    filter=~choosing,
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._toolbar_text), height=1), filter=busy | choosing
                ),
            ]
        )

        output = output or create_output()
        if isinstance(output, Vt100_Output):
            # A cursor position report makes prompt_toolkit size its frame to
            # everything below the cursor and pin the input to that row; without
            # it the frame is as tall as its content and output flows downward.
            output.enable_cpr = False
        self._app: Application[None] = Application(
            layout=Layout(layout, focused_element=self._input_window),
            key_bindings=merge_key_bindings(
                [
                    load_key_bindings(),
                    ConditionalKeyBindings(
                        merge_key_bindings([self._input_bindings(), key_bindings or KeyBindings()]),
                        filter=has_focus(self._buffer),
                    ),
                    self._run_bindings(busy),
                    ConditionalKeyBindings(self._chooser_bindings(), filter=choosing),
                ]
            ),
            style=PROMPT_STYLE,
            full_screen=False,
            erase_when_done=True,
            input=input,
            output=output,
        )
        self._app.ttimeoutlen = _ESCAPE_FLUSH_TIMEOUT
        self._app.timeoutlen = _KEY_SEQUENCE_TIMEOUT

    # -- Public API ------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """Whether a run is active: enables Esc/Ctrl+C cancel, the toolbar, and spinner animation."""

        return self._busy

    @busy.setter
    def busy(self, value: bool) -> None:
        self._busy = value
        if not value:
            self._tail_rows = 0
        if self._animation is not None:
            self._animation.cancel()
            self._animation = None
        if value and self._app.is_running:
            self._animation = self._app.create_background_task(self._animate())
        self._app.invalidate()

    @property
    def width(self) -> int:
        """Current terminal width in columns."""

        return self._app.output.get_size().columns

    @property
    def tail_height(self) -> int:
        """Maximum number of lines the tail area may use.

        Half the screen at most: when the terminal shrinks, emulators push the
        top of the redrawn area into the scrollback, so a smaller area survives
        a bigger shrink without garbled lines.
        """

        return max(3, self._app.output.get_size().rows // 2)

    async def run(self, main: Callable[[], Awaitable[None]]) -> None:
        """Run the application while ``main`` runs; exceptions from ``main`` propagate."""

        task: asyncio.Task[None] | None = None
        handler = _LogHandler(self, asyncio.get_running_loop())

        async def body() -> None:
            try:
                await main()
            finally:
                await self.flush()
                future = self._app.future
                if future is not None and not future.done():
                    self._app.exit()

        def start() -> None:
            nonlocal task
            task = asyncio.create_task(body())

        logging.getLogger().addHandler(handler)
        try:
            await self._app.run_async(pre_run=start)
        finally:
            logging.getLogger().removeHandler(handler)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if task is not None:
            task.result()

    async def read(self) -> str:
        """Return the next submitted input, waiting if needed; raise EOFError on Ctrl+D."""

        text = await self._inputs.get()
        self._app.invalidate()
        if text is None:
            raise EOFError
        return text

    def set_input(self, text: str) -> None:
        """Replace the input buffer text, with the cursor at the end."""

        self._buffer.text = text
        self._buffer.cursor_position = len(text)

    def prepend_input(self, text: str) -> None:
        """Put text ahead of the input draft, a blank line between them, with the cursor at the end."""

        draft = self._buffer.text
        self.set_input(f"{text}\n\n{draft}" if draft.strip() else text)

    def set_pending(self, lines: Sequence[Text]) -> None:
        """Replace the pending lines shown above the input."""

        self._pending_input = list(lines)
        self._app.invalidate()

    def print(self, *renderables: RenderableType) -> None:
        """Queue renderables for the scrollback, each on its own line; no arguments prints a blank line.

        Strings are printed as plain text, never as markup. Writes happen in
        order, immediately when the application is not running.
        """

        text = _render(renderables or ("",), self.width)
        if not self._app.is_running:
            self._app.output.write_raw(text)
            self._app.output.flush()
            return
        self._pending.append(text)
        if self._writer is None:
            self._writer = asyncio.get_running_loop().create_task(self._write_pending())

    async def flush(self) -> None:
        """Wait until every queued print is written."""

        if self._writer is not None:
            await asyncio.shield(self._writer)

    def set_tail(self, renderable: RenderableType | None) -> None:
        """Replace the tail area content; the change shows after already queued prints."""

        if self._writer is not None:
            self._next_tail = (renderable,)
            return
        self._tail = renderable
        self._app.invalidate()

    async def choose[T](
        self,
        options: Sequence[tuple[T, str | Text] | str],
        *,
        default: T | None = None,
        query: str = "",
    ) -> T | None:
        """Show an inline, filterable list and return the chosen value, or None on cancel.

        Options are ``(value, label)`` pairs; a bare string is a heading that
        groups the options after it. Typing filters the options by label and
        heading, starting from ``query``.
        """

        rows: list[_Row] = []
        values: list[T | None] = []
        heading = ""
        for option in options:
            if isinstance(option, str):
                heading = option
                rows.append(_Row(Text(option, style=MUTED), heading=True, search=""))
                values.append(None)
            else:
                value, label = option
                label = Text(label) if isinstance(label, str) else label
                rows.append(_Row(label, heading=False, search=f"{heading} {label.plain}".lower()))
                values.append(value)
        if all(row.heading for row in rows):
            return None
        future: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
        choice = _Choice(rows, None, future, query)
        selectable = choice.selectable()
        choice.index = next((i for i in selectable if values[i] == default), next(iter(selectable), None))
        self._choice = choice
        self._app.layout.focus(self._chooser_window)
        self._app.invalidate()
        try:
            picked = await future
        finally:
            self._choice = None
            self._app.layout.focus(self._input_window)
            self._app.invalidate()
        return None if picked is None else values[picked]

    # -- Output ----------------------------------------------------------------

    def _write(self, text: str) -> None:
        """Write text into the scrollback above the application and redraw it in one flush.

        This is ``Renderer.erase`` followed by a full render, without the flush
        in between: the erased area, the new text, and the redrawn frame reach
        the terminal in a single write, so the bottom area never blanks.
        ``run_in_terminal`` would flush, switch the tty mode, and wait for a
        cursor position report on every commit.
        """

        renderer = self._app.renderer
        output = self._app.output
        # `_cursor_pos` is where the renderer left the cursor inside its frame.
        output.cursor_backward(renderer._cursor_pos.x)  # pyright: ignore[reportPrivateUsage]
        output.cursor_up(renderer._cursor_pos.y)  # pyright: ignore[reportPrivateUsage]
        output.erase_down()
        output.write_raw(text)
        renderer._cursor_pos = Point(x=0, y=0)  # pyright: ignore[reportPrivateUsage]
        renderer._last_screen = None  # pyright: ignore[reportPrivateUsage]
        if self._next_tail is not None:
            (self._tail,) = self._next_tail
            self._next_tail = None
        # Controls cache their content per counter value; a stale count would redraw the old tail.
        self._app.render_counter += 1
        renderer.render(self._app, self._app.layout)

    async def _write_pending(self) -> None:
        """Write the prints queued during this event-loop iteration as one batch."""

        try:
            text = "".join(self._pending)
            self._pending.clear()
            self._write(text)
        finally:
            self._writer = None

    async def _animate(self) -> None:
        while True:
            await asyncio.sleep(_ANIMATION_INTERVAL)
            self._app.invalidate()

    # -- Layout content --------------------------------------------------------

    def _tail_text(self) -> ANSI:
        lines: list[str] = []
        if self._tail is not None:
            lines = _render([self._tail], self.width).rstrip("\n").split("\n")[-self.tail_height :]
        return ANSI(_HYPERLINK.sub("", "\n".join(self._reserve(lines))))

    def _reserve(self, lines: list[str]) -> list[str]:
        """Pad the tail area to the tallest it has been during this run."""

        if self._busy:
            self._tail_rows = min(max(self._tail_rows, len(lines)), self.tail_height)
        return lines + [""] * (self._tail_rows - len(lines))

    def _chooser_text(self) -> StyleAndTextTuples:
        choice = self._choice
        if choice is None:
            return []
        visible = choice.visible()
        rows: list[Text] = []
        for index in visible:
            row = choice.rows[index]
            if row.heading:
                text = row.label.copy()
            else:
                focused = index == choice.index
                text = Text(f"{CHOICE_MARKER if focused else ' '} ", style=ACCENT if focused else "")
                text.append_text(row.label)
                if focused:
                    text.stylize(ACCENT)
            text.no_wrap = True
            text.overflow = "ellipsis"
            rows.append(text)
        if not rows:
            rows.append(Text("  no matches", style=MUTED))
        focus = None if choice.index is None else visible.index(choice.index)
        fragments: StyleAndTextTuples = []
        for position, line in enumerate(self._reserve(_render(rows, self.width).rstrip("\n").split("\n"))):
            if position:
                fragments.append(("", "\n"))
            if position == focus:
                # Keeps the focused row scrolled into view.
                fragments.append(("[SetCursorPosition]", ""))
            fragments.extend(to_formatted_text(ANSI(line)))
        return fragments

    def _toolbar_text(self) -> ANSI:
        """The keys that work right now."""

        if self._choice is not None:
            text = Text()
            if self._choice.query:
                text.append(f"{self._choice.query}  ")
            text.append("type to filter · ↑↓ select · enter confirm · esc cancel", style=MUTED)
        else:
            text = Text("esc to interrupt", style=MUTED)
            if self.on_submit is not None:
                text.append(" · ctrl+q queue", style=MUTED)
            if self._pending_input:
                text.append(" · alt+↑ take back", style=MUTED)
        return ANSI(_render([text], self.width, end=""))

    def _pending_text(self) -> ANSI:
        return ANSI(_render(self._pending_input, self.width).rstrip("\n"))

    def _line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        if line_number == 0 and wrap_count == 0:
            return self._prompt
        return [("", "  ")]

    def _input_height(self) -> Dimension:
        if self._buffer.complete_state is not None:
            return Dimension(min=min(_MENU_RESERVED_LINES, self._app.output.get_size().rows // 3))
        return Dimension()

    # -- Keys ------------------------------------------------------------------

    def _accept(self, buffer: Buffer) -> bool:
        return self._submit(buffer.text, queue=False)

    def _submit(self, text: str, *, queue: bool) -> bool:
        """Hand submitted text to the run while busy, else to ``read``; return whether the input keeps it."""

        if not text.strip():
            return False
        if self._busy:
            # Without a taker (e.g. during /compact) the text stays in the input.
            return self.on_submit is None or not self.on_submit(text, queue)
        self._inputs.put_nowait(text)
        return False

    def _input_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event: KeyPressEvent) -> None:
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        def _newline(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("c-q")
        def _queue(event: KeyPressEvent) -> None:
            buffer = event.current_buffer
            if not self._submit(buffer.text, queue=True):
                buffer.append_to_history()
                buffer.reset()

        @kb.add("c-d", filter=Condition(lambda: not self._buffer.text))
        def _eof(_event: KeyPressEvent) -> None:
            self._inputs.put_nowait(None)

        @kb.add("c-l")
        def _clear(_event: KeyPressEvent) -> None:
            self._app.renderer.clear()
            self._app.invalidate()

        return kb

    def _run_bindings(self, busy: Condition) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("escape", filter=busy)
        def _cancel(_event: KeyPressEvent) -> None:
            if self.on_cancel is not None:
                self.on_cancel()

        @kb.add("escape", "up", filter=busy)
        def _take_back(_event: KeyPressEvent) -> None:
            if self.on_take_back is not None:
                self.on_take_back()

        @kb.add("c-c")
        @kb.add("<sigint>")
        def _interrupt(_event: KeyPressEvent) -> None:
            if self._busy:
                if self.on_cancel is not None:
                    self.on_cancel()
            else:
                self._buffer.reset()

        return kb

    def _chooser_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        def move(step: int) -> None:
            choice = self._choice
            if choice is None or choice.index is None:
                return
            selectable = choice.selectable()
            position = min(max(selectable.index(choice.index) + step, 0), len(selectable) - 1)
            choice.index = selectable[position]

        def pick(index: int | None) -> None:
            if self._choice is not None and not self._choice.future.done():
                self._choice.future.set_result(index)

        @kb.add("up")
        def _up(_event: KeyPressEvent) -> None:
            move(-1)

        @kb.add("down")
        def _down(_event: KeyPressEvent) -> None:
            move(1)

        @kb.add("enter")
        def _select(_event: KeyPressEvent) -> None:
            if self._choice is not None and self._choice.index is not None:
                pick(self._choice.index)

        @kb.add(Keys.Any)
        def _type(event: KeyPressEvent) -> None:
            if self._choice is not None and event.data.isprintable():
                self._choice.set_query(self._choice.query + event.data)

        @kb.add("backspace")
        def _erase(_event: KeyPressEvent) -> None:
            if self._choice is not None:
                self._choice.set_query(self._choice.query[:-1])

        # Eager: nothing in the chooser continues an Esc sequence.
        @kb.add("escape", eager=True)
        @kb.add("c-c")
        @kb.add("<sigint>")
        def _dismiss(_event: KeyPressEvent) -> None:
            pick(None)

        return kb
