"""Shared fixtures for the TUI tests."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.completion import DummyCompleter
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import PipeInput
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from mycode_cli.tui.terminal import Terminal

_ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[=>])")


@dataclass
class TerminalHarness:
    """A Terminal driven through a pipe, with its VT100 output captured."""

    terminal: Terminal
    pipe: PipeInput
    output: StringIO

    def text(self) -> str:
        """Everything written so far, without ANSI sequences and the line padding rich adds."""

        return re.sub(r" +\n", "\n", _ANSI.sub("", self.output.getvalue()))


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[TerminalHarness]:
    with create_pipe_input() as pipe:
        output = StringIO()
        terminal = Terminal(
            history_path=str(tmp_path / "history"),
            completer=DummyCompleter(),
            input=pipe,
            output=Vt100_Output(stdout=output, get_size=lambda: Size(rows=24, columns=80), term="xterm-256color"),
        )
        yield TerminalHarness(terminal, pipe, output)
