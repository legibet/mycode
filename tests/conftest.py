"""Shared fixtures and fakes for the tests."""

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

from mycode.messages import ConversationMessage
from mycode_cli.tui.terminal import Terminal

_ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[=>])")
# Esc waits for a possible Esc+Enter before it counts as a lone key.
ESC_WAIT = 0.8


class FakeAgent:
    """Base for run manager fakes: model facts, and no steers accepted."""

    model = "test-model"
    context_window = 1_000
    supports_image_input = True
    supports_pdf_input = True

    messages: list[ConversationMessage] = []

    def steer(self, message: ConversationMessage) -> bool:
        return False

    def pending_steers(self) -> list[ConversationMessage]:
        return []


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
