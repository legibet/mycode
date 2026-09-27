"""Tests for the TUI renderables and the per-turn event renderer."""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any, cast

import pytest
from conftest import TerminalHarness
from rich.console import Console

from mycode.agent import Event
from mycode_cli.tui.render import TurnRenderer, history_preview
from mycode_cli.tui.theme import ERROR_MARKER, THINKING_SYMBOL, TOOL_MARKER


def test_history_preview_renders_recent_turns() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)

    for renderable in history_preview(
        [
            {"role": "user", "content": [{"type": "text", "text": "older question"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": [{"type": "text", "text": "turn one"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "check @main.py"},
                    {
                        "type": "text",
                        "text": '<file name="/tmp/main.py">\nprint(1)\n</file>',
                        "meta": {"attachment": True, "path": "/tmp/main.py"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "hidden"},
                    {"type": "text", "text": "checking `foo`"},
                    {"type": "tool_use", "name": "read", "input": {"path": "foo.py"}},
                    {"type": "tool_use", "id": "e1", "name": "edit", "input": {"path": "b.py"}},
                    {"type": "text", "text": "```py\nprint(1)\n```"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "e1",
                        "output": "ok",
                        "metadata": {"added_lines": 3, "removed_lines": 1},
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "latest question"}]},
        ],
        width=120,
    ):
        console.print(renderable)

    rendered = output.getvalue()
    assert "older question" not in rendered
    assert "older answer" not in rendered
    assert '<file name="/tmp/main.py">' not in rendered
    assert "turn one" in rendered
    assert "check @main.py" in rendered
    assert "checking foo" in rendered
    assert "Read  foo.py" in rendered
    # Tool lines read the stored result, as during the live turn.
    assert "Edit  b.py  +3 −1" in rendered
    assert "print(1)" in rendered
    assert "latest question" in rendered


class _EventAgent:
    """Fake agent that streams a fixed list of events, pausing between them."""

    def __init__(self, events: list[Event], *, delay: float = 0.0) -> None:
        self.events = events
        self.delay = delay

    async def achat(self, message: str, *, on_persist=None):
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield event


def _text_events(text: str, size: int = 7) -> list[Event]:
    return [Event("text", {"delta": text[index : index + size]}) for index in range(0, len(text), size)]


async def _render_turn(
    harness: TerminalHarness,
    events: list[Event],
    *,
    delay: float = 0.0,
    model: str = "m",
    context_window: int | None = None,
    session_cost_base: float | None = None,
) -> tuple[int, str]:
    renderer = TurnRenderer(
        harness.terminal, model=model, context_window=context_window, session_cost_base=session_cost_base
    )
    code = await renderer.render(cast(Any, _EventAgent(events, delay=delay)), "hi")
    return code, harness.text()


class TestTurnRenderer:
    async def test_streamed_markdown_is_printed_once_in_order(self, harness: TerminalHarness) -> None:
        paragraphs = [f"Paragraph {index} has a few words in it." for index in range(30)]
        document = (
            "# Title\n\n"
            + "\n\n".join(paragraphs)
            + "\n\n```py\n"
            + "\n".join(f"value_{index} = {index}" for index in range(40))
            + "\n```\n"
        )

        code, rendered = await _render_turn(harness, _text_events(document), delay=0.002)

        assert code == 0
        for needle in ["Title", *paragraphs, *(f"value_{index} = {index}" for index in range(40))]:
            assert rendered.count(needle) == 1, needle
        positions = [rendered.index(needle) for needle in ["Title", paragraphs[0], paragraphs[-1], "value_39"]]
        assert positions == sorted(positions)
        # Blocks keep the single blank line between them.
        assert "Paragraph 0 has a few words in it.\n\nParagraph 1" in rendered

    async def test_thinking_collapses_into_a_summary_line(self, harness: TerminalHarness) -> None:
        code, rendered = await _render_turn(
            harness,
            [
                Event("reasoning", {"delta": "Let me think"}),
                Event("reasoning_done", {"duration_ms": 1200}),
                Event("text", {"delta": "answer"}),
            ],
        )

        assert code == 0
        assert "Let me think" not in rendered
        assert f"{THINKING_SYMBOL} thought · 1.2s\n\nanswer" in rendered

    async def test_tool_output_chunks_are_joined_into_lines(self, harness: TerminalHarness) -> None:
        code, rendered = await _render_turn(
            harness,
            [
                Event("tool_start", {"tool_call": {"id": "1", "name": "bash", "input": {"command": "printf"}}}),
                Event("tool_output", {"tool_use_id": "1", "output": "one\nsec"}),
                Event("tool_output", {"tool_use_id": "1", "output": "ond\nthird"}),
                Event("tool_done", {"tool_use_id": "1", "output": "one\nsecond\nthird", "is_error": False}),
            ],
        )

        assert code == 0
        assert f"{TOOL_MARKER} Bash  printf\n  one\n  second\n  third\n" in rendered

    async def test_tool_output_keeps_the_last_lines(self, harness: TerminalHarness) -> None:
        lines = "".join(f"line {index}\n" for index in range(8))
        _, rendered = await _render_turn(
            harness,
            [
                Event("tool_start", {"tool_call": {"id": "1", "name": "bash", "input": {"command": "seq"}}}),
                Event("tool_output", {"tool_use_id": "1", "output": lines}),
                Event("tool_done", {"tool_use_id": "1", "output": lines, "is_error": False}),
            ],
        )

        assert "  … +3 lines\n  line 3\n  line 4\n  line 5\n  line 6\n  line 7\n" in rendered
        assert "line 2" not in rendered

    async def test_tool_output_is_shown_as_a_terminal_would(self, harness: TerminalHarness) -> None:
        output = "\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\\n10%\r100%\n" + "x" * 200 + "\n"
        _, rendered = await _render_turn(
            harness,
            [
                Event(
                    "tool_start",
                    {"tool_call": {"id": "1", "name": "bash", "input": {"command": "make\n  && make test"}}},
                ),
                Event("tool_output", {"tool_use_id": "1", "output": output}),
                Event("tool_done", {"tool_use_id": "1", "output": output, "is_error": False}),
            ],
        )

        # One header line, escapes and overwritten progress removed, long lines cut to the width.
        assert f"{TOOL_MARKER} Bash  make && make test\n  link\n  100%\n  {'x' * 77}…\n" in rendered

    async def test_tool_header_fits_one_line_and_keeps_the_suffix(self, harness: TerminalHarness) -> None:
        path = "src/" + "deep/" * 30 + "file.py"
        _, rendered = await _render_turn(
            harness,
            [
                Event("tool_start", {"tool_call": {"id": "1", "name": "bash", "input": {"command": "rg -n x " * 20}}}),
                Event("tool_done", {"tool_use_id": "1", "output": "", "is_error": False}),
                Event("tool_start", {"tool_call": {"id": "2", "name": "edit", "input": {"path": path}}}),
                Event(
                    "tool_done",
                    {
                        "tool_use_id": "2",
                        "output": "ok",
                        "is_error": False,
                        "metadata": {"added_lines": 3, "removed_lines": 1},
                    },
                ),
            ],
        )

        bash, edit = [line for line in rendered.splitlines() if line.startswith(TOOL_MARKER)]
        assert bash.endswith("…")
        # A path is cut at the front so the file name and the suffix stay visible.
        assert edit.startswith(f"{TOOL_MARKER} Edit  …")
        assert edit.endswith("deep/file.py  +3 −1")

    async def test_tool_done_shows_final_status_after_live_output(self, harness: TerminalHarness) -> None:
        code, rendered = await _render_turn(
            harness,
            [
                Event("tool_start", {"tool_call": {"id": "1", "name": "bash", "input": {"command": "build"}}}),
                Event("tool_output", {"tool_use_id": "1", "output": "started\n"}),
                Event(
                    "tool_done",
                    {
                        "tool_use_id": "1",
                        "output": "started\n\n[Output truncated: Showing the last 50KB of output. "
                        + "Full output: /tmp/bash.log.]\n\n[Command timed out after 1s]",
                        "is_error": True,
                    },
                ),
            ],
        )

        assert code == 1
        assert "[Output truncated: Showing the last 50KB of output." in rendered
        assert "/tmp/bash.log.]" in rendered
        assert "Command timed out after 1s" in rendered

    async def test_buffered_tools_get_a_success_suffix(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                Event(
                    "tool_start",
                    {"tool_call": {"id": "1", "name": "read", "input": {"path": "a.py", "offset": 10, "limit": 5}}},
                ),
                Event("tool_done", {"tool_use_id": "1", "output": "x", "is_error": False}),
                Event("tool_start", {"tool_call": {"id": "2", "name": "edit", "input": {"path": "b.py"}}}),
                Event(
                    "tool_done",
                    {
                        "tool_use_id": "2",
                        "output": "Updated b.py",
                        "is_error": False,
                        "metadata": {"added_lines": 3, "removed_lines": 1},
                    },
                ),
            ],
        )

        assert f"{TOOL_MARKER} Read  a.py  :10-15\n" in rendered
        assert f"{TOOL_MARKER} Edit  b.py  +3 −1\n" in rendered

    async def test_finish_prints_context_and_session_cost(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [Event("usage", {"context_tokens": 34_210, "turn_cost": {"total": 0.02}})],
            model="gpt-5.5",
            context_window=128_000,
            session_cost_base=0.40,
        )

        assert "gpt-5.5  34,210 tokens (27%) · $0.42" in rendered

    @pytest.mark.parametrize(
        ("session_cost_base", "turn_cost", "expected"),
        [
            pytest.param(None, 0.02, 0.02, id="turn-only"),
            pytest.param(0.40, None, 0.40, id="history-only"),
            pytest.param(None, None, None, id="all-unknown"),
        ],
    )
    async def test_finish_combines_known_costs(
        self,
        harness: TerminalHarness,
        session_cost_base: float | None,
        turn_cost: float | None,
        expected: float | None,
    ) -> None:
        usage = {"turn_cost": {"total": turn_cost} if turn_cost is not None else None}
        _, rendered = await _render_turn(
            harness,
            [Event("usage", usage)],
            context_window=1_000,
            session_cost_base=session_cost_base,
        )

        if expected is None:
            assert "$" not in rendered
        else:
            assert f"${expected:.2f}" in rendered

    async def test_cancelled_event_is_a_muted_stop_not_an_error(self, harness: TerminalHarness) -> None:
        code, rendered = await _render_turn(harness, [Event("text", {"delta": "partial"}), Event("cancelled", {})])

        assert code == 0
        assert "partial\ncancelled\n" in rendered
        assert ERROR_MARKER not in rendered

    async def test_error_event_is_printed_and_fails_the_turn(self, harness: TerminalHarness) -> None:
        code, rendered = await _render_turn(harness, [Event("error", {"message": "provider error"})])

        assert code == 1
        assert f"{ERROR_MARKER} provider error" in rendered

    async def test_renders_inside_a_running_terminal(self, harness: TerminalHarness) -> None:
        renderer = TurnRenderer(harness.terminal, model="m", context_window=None)
        codes: list[int] = []

        async def main() -> None:
            agent = _EventAgent([*_text_events("first block\n\nsecond block\n"), Event("usage", {"context_tokens": 5})])
            codes.append(await renderer.render(cast(Any, agent), "hi"))

        await harness.terminal.run(main)

        rendered = harness.text()
        assert codes == [0]
        assert "first block\n" in rendered
        assert "second block\n" in rendered
        assert "m  5 tokens" in rendered
