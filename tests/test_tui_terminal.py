"""Tests for the TUI terminal owner (input queue, cancel keys, chooser, scrollback output)."""

from __future__ import annotations

import asyncio
import logging
from io import StringIO
from pathlib import Path

import pytest
from conftest import TerminalHarness
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.text import Text

from mycode_cli.tui import terminal as terminal_module
from mycode_cli.tui.markdown import MarkdownBlock
from mycode_cli.tui.terminal import Terminal

# Esc waits for a possible Esc+Enter before it counts as a lone key.
ESC_WAIT = 0.8


async def test_print_before_run_writes_immediately(harness: TerminalHarness) -> None:
    harness.terminal.print("hello", Text("world"))

    assert harness.text() == "hello\nworld\n"


async def test_print_treats_strings_as_plain_text(harness: TerminalHarness) -> None:
    harness.terminal.print("[red]model[/red] [effort: high]")

    assert harness.text() == "[red]model[/red] [effort: high]\n"


async def test_prints_during_run_keep_their_order(harness: TerminalHarness) -> None:
    harness.terminal.print("before")

    async def main() -> None:
        harness.terminal.print("one")
        harness.terminal.print("two", "three")
        await harness.terminal.flush()
        harness.terminal.print("four")

    await harness.terminal.run(main)
    harness.terminal.print("after")

    # Redraws of the input area are interleaved, so check order by position.
    rendered = harness.text()
    positions = [rendered.index(f"{word}\n") for word in ("before", "one", "two", "three", "four", "after")]
    assert positions == sorted(positions)


async def test_run_propagates_errors_from_main(harness: TerminalHarness) -> None:
    async def main() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await harness.terminal.run(main)


async def test_enter_submits_and_esc_enter_inserts_newline(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe
    results: list[str] = []

    async def main() -> None:
        pipe.send_text("first\r")
        results.append(await terminal.read())
        pipe.send_text("two\x1b\rlines\r")
        results.append(await terminal.read())

    await terminal.run(main)

    assert results == ["first", "two\nlines"]


async def test_enter_while_busy_queues_inputs_in_order(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe
    results: list[str | int] = []

    async def main() -> None:
        terminal.busy = True
        pipe.send_text("one\r")
        pipe.send_text("two\r")
        await asyncio.sleep(0.2)
        results.append(terminal.queued())
        terminal.busy = False
        results.append(await terminal.read())
        results.append(await terminal.read())
        results.append(terminal.queued())

    await terminal.run(main)

    assert results == [2, "one", "two", 0]


async def test_esc_cancels_only_while_busy(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe
    cancels: list[str] = []
    terminal.on_cancel = lambda: cancels.append("cancel")

    async def main() -> None:
        pipe.send_text("\x1b")
        await asyncio.sleep(ESC_WAIT)
        assert cancels == []
        terminal.busy = True
        pipe.send_text("\x1b")
        await asyncio.sleep(ESC_WAIT)
        terminal.busy = False

    await terminal.run(main)

    assert cancels == ["cancel"]


async def test_ctrl_c_cancels_while_busy_and_clears_input_otherwise(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe
    cancels: list[str] = []
    terminal.on_cancel = lambda: cancels.append("cancel")
    results: list[str] = []

    async def main() -> None:
        terminal.busy = True
        pipe.send_text("\x03")
        await asyncio.sleep(0.2)
        terminal.busy = False
        pipe.send_text("draft\x03kept\r")
        results.append(await terminal.read())

    await terminal.run(main)

    assert cancels == ["cancel"]
    assert results == ["kept"]


async def test_ctrl_d_on_empty_input_raises_eof(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe

    async def main() -> None:
        pipe.send_text("\x04")
        await terminal.read()

    with pytest.raises(EOFError):
        await terminal.run(main)


async def test_choose_selects_with_arrows_and_enter(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe
    results: list[str | None] = []
    options = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]

    async def main() -> None:
        async def keys() -> None:
            await asyncio.sleep(0.1)
            pipe.send_text("\x1b[B")
            await asyncio.sleep(0.05)
            pipe.send_text("\r")

        task = asyncio.create_task(keys())
        results.append(await terminal.choose(options, default="b"))
        await task

    await terminal.run(main)

    assert results == ["c"]


async def test_choose_returns_none_on_esc_and_keeps_the_draft(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe
    results: list[str | None] = []

    async def main() -> None:
        pipe.send_text("draft")
        await asyncio.sleep(0.1)

        async def keys() -> None:
            await asyncio.sleep(0.1)
            pipe.send_text("\x1b")

        task = asyncio.create_task(keys())
        results.append(await terminal.choose([("a", "Alpha"), ("b", "Beta")]))
        await task
        pipe.send_text("\r")
        results.append(await terminal.read())

    await terminal.run(main)

    assert results == [None, "draft"]


async def test_choose_cuts_long_labels_to_one_row(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe

    async def main() -> None:
        async def keys() -> None:
            await asyncio.sleep(0.1)
            pipe.send_text("\x1b")

        task = asyncio.create_task(keys())
        await terminal.choose([("a", "y" * 200)])
        await task

    await terminal.run(main)

    rendered = harness.text()
    assert "❯ " + "y" * 77 in rendered
    assert "y" * 78 not in rendered
    assert "…" in rendered


async def test_tail_shows_the_last_lines(harness: TerminalHarness) -> None:
    async def main() -> None:
        harness.terminal.set_tail(Text("\n".join(f"row {index}" for index in range(40))))
        await asyncio.sleep(0.2)

    await harness.terminal.run(main)

    rendered = harness.text()
    assert "row 39" in rendered
    assert f"row {40 - harness.terminal.tail_height}" in rendered
    assert f"row {39 - harness.terminal.tail_height}\r" not in rendered
    assert "row 0\r" not in rendered


async def test_toolbar_shows_interrupt_hint_and_queue_while_busy(harness: TerminalHarness) -> None:
    terminal, pipe = harness.terminal, harness.pipe

    async def main() -> None:
        terminal.busy = True
        pipe.send_text("later\r")
        await asyncio.sleep(0.3)
        terminal.busy = False
        await terminal.read()

    await terminal.run(main)

    assert "esc to interrupt · 1 queued" in harness.text()


async def test_commit_redraws_the_new_tail_in_the_same_write(harness: TerminalHarness) -> None:
    async def main() -> None:
        harness.terminal.set_tail(Text("prefix\nold tail"))
        await asyncio.sleep(0.1)
        harness.output.seek(0)
        harness.output.truncate()
        harness.terminal.print("prefix")
        harness.terminal.set_tail(Text("new tail"))
        await harness.terminal.flush()

    await harness.terminal.run(main)

    # The frame drawn with the commit must not repeat the tail the commit replaced.
    assert "old tail" not in harness.text()


async def test_log_records_go_through_the_terminal(harness: TerminalHarness) -> None:
    async def main() -> None:
        # Sync tools run in worker threads; their records must reach the loop thread first.
        await asyncio.to_thread(logging.getLogger("mycode.test").warning, "from a worker thread")
        try:
            raise ValueError("bad")
        except ValueError:
            logging.getLogger("mycode.test").exception("Provider request failed")
        await harness.terminal.flush()

    await harness.terminal.run(main)

    assert "error: Provider request failed: ValueError('bad')" in harness.text()
    assert "warning: from a worker thread" in harness.text()


async def test_tail_drops_hyperlink_sequences(harness: TerminalHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_module, "_COLOR_SYSTEM", "truecolor")

    async def main() -> None:
        harness.terminal.set_tail(MarkdownBlock("See [docs](https://example.com/docs)."))
        await asyncio.sleep(0.1)

    await harness.terminal.run(main)

    assert "See docs." in harness.text()
    assert "8;id=" not in harness.text()


async def test_completion_menu_fits_a_short_terminal(tmp_path: Path) -> None:
    output = StringIO()
    with create_pipe_input() as pipe:
        terminal = Terminal(
            history_path=str(tmp_path / "history"),
            completer=WordCompleter(["/help", "/compact"]),
            input=pipe,
            output=Vt100_Output(stdout=output, get_size=lambda: Size(rows=8, columns=80), term="xterm-256color"),
        )

        async def main() -> None:
            terminal.busy = True
            terminal.set_tail(Text("a\nb\nc\nd"))
            pipe.send_text("/")
            await asyncio.sleep(0.2)
            terminal.busy = False

        await terminal.run(main)

    assert "too small" not in output.getvalue()
    assert "/help" in output.getvalue()
