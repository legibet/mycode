"""The terminal owner for the interactive chat.

``Terminal`` runs one persistent, non-fullscreen prompt_toolkit application.
Finished output goes to the terminal scrollback above it; only the bottom area
(tail, chooser, input, toolbar) is redrawn. Nothing else writes to the terminal
while it runs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
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
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import DisplayMultipleCursors, HighlightSelectionProcessor
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console, RenderableType
from rich.text import Text

from .theme import ACCENT, ERROR, MARKDOWN_THEME, MUTED, PROMPT_CHAR, SELECTED

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


@dataclass
class _Choice:
    labels: list[str]
    index: int
    future: asyncio.Future[int | None]


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
        style = ERROR if record.levelno >= logging.ERROR else MUTED
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
        self._busy = False
        self._animation: asyncio.Task[None] | None = None
        # Submitted inputs; None marks Ctrl+D.
        self._inputs: asyncio.Queue[str | None] = asyncio.Queue()
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
                        HSplit([Window(height=1), self._input_window]),
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
                ConditionalContainer(Window(FormattedTextControl(self._toolbar_text), height=1), filter=busy),
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

    def queued(self) -> int:
        """Return the number of submitted inputs not yet returned by ``read``."""

        return self._inputs.qsize()

    def set_input(self, text: str) -> None:
        """Replace the input buffer text, with the cursor at the end."""

        self._buffer.text = text
        self._buffer.cursor_position = len(text)

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

    async def choose[T](self, options: list[tuple[T, str]], *, default: T | None = None) -> T | None:
        """Show an inline list of ``(value, label)`` options; return the chosen value or None on cancel."""

        if not options:
            return None
        index = next((i for i, (value, _) in enumerate(options) if value == default), 0)
        future: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
        self._choice = _Choice([label for _, label in options], index, future)
        self._app.layout.focus(self._chooser_window)
        self._app.invalidate()
        try:
            picked = await future
        finally:
            self._choice = None
            self._app.layout.focus(self._input_window)
            self._app.invalidate()
        return None if picked is None else options[picked][0]

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
        rows = [
            Text(
                f"{'>' if index == choice.index else ' '} {label}",
                style=SELECTED if index == choice.index else "",
                no_wrap=True,
                overflow="ellipsis",
            )
            for index, label in enumerate(choice.labels)
        ]
        fragments: StyleAndTextTuples = []
        for index, line in enumerate(self._reserve(_render(rows, self.width).rstrip("\n").split("\n"))):
            if index:
                fragments.append(("", "\n"))
            if index == choice.index:
                # Keeps the focused row scrolled into view.
                fragments.append(("[SetCursorPosition]", ""))
            fragments.extend(to_formatted_text(ANSI(line)))
        return fragments

    def _toolbar_text(self) -> ANSI:
        text = Text("esc to interrupt", style=MUTED)
        if queued := self.queued():
            text.append(f" · {queued} queued", style=MUTED)
        return ANSI(_render([text], self.width, end=""))

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
        if buffer.text.strip():
            self._inputs.put_nowait(buffer.text)
        return False

    def _input_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event: KeyPressEvent) -> None:
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        def _newline(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

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
            if self._choice is not None:
                self._choice.index = min(max(self._choice.index + step, 0), len(self._choice.labels) - 1)

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
            if self._choice is not None:
                pick(self._choice.index)

        # Eager: nothing in the chooser continues an Esc sequence.
        @kb.add("escape", eager=True)
        @kb.add("c-c")
        @kb.add("<sigint>")
        def _dismiss(_event: KeyPressEvent) -> None:
            pick(None)

        return kb
