"""Shared fixtures and fakes for the tests."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from PIL import Image
from prompt_toolkit.completion import DummyCompleter
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import PipeInput
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from pypdf import PdfWriter

from mycode.messages import ConversationMessage
from mycode_cli.tui.terminal import Terminal

_ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[=>])")
_STATUS = re.compile(r"\x1b\]7501;([^\x07]*)\x07")
# Esc waits for a possible Esc+Enter before it counts as a lone key.
ESC_WAIT = 0.8


def image_bytes(size: tuple[int, int], image_format: str = "PNG", mode: str = "RGB", **save: object) -> bytes:
    output = BytesIO()
    Image.new(mode, size).save(output, image_format, **save)
    return output.getvalue()


def pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


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

    def statuses(self) -> list[dict[str, str]]:
        """The program status reports (OSC 7501) written so far, with ``msg`` decoded."""

        reports = []
        for body in _STATUS.findall(self.output.getvalue()):
            report = dict(pair.split("=", 1) for pair in body.split(":"))
            if "msg" in report:
                report["msg"] = base64.b64decode(report["msg"]).decode()
            reports.append(report)
        return reports


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
