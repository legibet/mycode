"""Tests for the TUI renderables and the per-turn event renderer."""

from __future__ import annotations

import asyncio
import re
from io import StringIO
from typing import Any, cast

import pytest
from conftest import TerminalHarness
from rich.console import Console
from rich.text import Text

from mycode import (
    CancelledEvent,
    ErrorEvent,
    Event,
    ReasoningDoneEvent,
    ReasoningEvent,
    TextEvent,
    ToolDoneEvent,
    ToolOutputEvent,
    ToolStartEvent,
    UsageEvent,
    UserMessageEvent,
)
from mycode.messages import ConversationMessage, TextBlock, text_block
from mycode_cli.sessions import SessionTotals
from mycode_cli.tui.render import TurnRenderer, agent_transcript, history_preview, job_lines
from mycode_cli.tui.theme import ERROR, ERROR_MARKER, MUTED, PENDING_MARKER, THINKING_SYMBOL, TOOL_MARKER


def test_history_preview_renders_recent_turns() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)

    messages: list[ConversationMessage] = [
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
                {"type": "tool_use", "id": "r1", "name": "read", "input": {"path": "foo.py"}},
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
                    "is_error": False,
                    "metadata": {"added_lines": 3, "removed_lines": 1},
                }
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "latest question"}]},
    ]

    for renderable in history_preview(messages, width=120):
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
    return [TextEvent(delta=text[index : index + size]) for index in range(0, len(text), size)]


async def _render_turn(
    harness: TerminalHarness,
    events: list[Event],
    *,
    delay: float = 0.0,
    model: str = "m",
    context_window: int | None = None,
    session_cost_base: float | None = None,
) -> tuple[CancelledEvent | ErrorEvent | None, str]:
    session_base = SessionTotals(cost={"total": session_cost_base}) if session_cost_base is not None else None
    renderer = TurnRenderer(harness.terminal, model=model, context_window=context_window, session_base=session_base)
    ended = await renderer.render(cast(Any, _EventAgent(events, delay=delay)), "hi")
    return ended, harness.text()


def _agent_start(call_id: str, description: str, *, background: bool = False) -> ToolStartEvent:
    args: dict[str, Any] = {"description": description, "prompt": "do it"}
    if background:
        args["background"] = True
    return ToolStartEvent(tool_call={"type": "tool_use", "id": call_id, "name": "agent", "input": args})


def _agent_metadata(
    *, status: str = "completed", tool_calls: int = 1, duration_ms: int = 1000, cost: float | None = None
) -> dict[str, Any]:
    return {
        "agent": "general",
        "session_id": "1",
        "background": False,
        "status": status,
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "cost": None if cost is None else {"total": cost},
    }


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

        ended, rendered = await _render_turn(harness, _text_events(document), delay=0.002)

        assert ended is None
        for needle in ["Title", *paragraphs, *(f"value_{index} = {index}" for index in range(40))]:
            assert rendered.count(needle) == 1, needle
        positions = [rendered.index(needle) for needle in ["Title", paragraphs[0], paragraphs[-1], "value_39"]]
        assert positions == sorted(positions)
        # Blocks keep the single blank line between them.
        assert "Paragraph 0 has a few words in it.\n\nParagraph 1" in rendered

    async def test_thinking_collapses_into_a_summary_line(self, harness: TerminalHarness) -> None:
        ended, rendered = await _render_turn(
            harness,
            [
                ReasoningEvent(delta="Let me think"),
                ReasoningDoneEvent(duration_ms=1200),
                TextEvent(delta="answer"),
            ],
        )

        assert ended is None
        assert "Let me think" not in rendered
        assert f"{THINKING_SYMBOL} thought · 1.2s\n\nanswer" in rendered

    async def test_tool_output_chunks_are_joined_into_lines(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(
                    tool_call={"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "printf"}}
                ),
                ToolOutputEvent(tool_use_id="1", output="one\nsec"),
                ToolOutputEvent(tool_use_id="1", output="ond\nthird"),
                ToolDoneEvent(tool_use_id="1", output="one\nsecond\nthird", is_error=False),
            ],
        )

        assert f"{TOOL_MARKER} Bash  printf\n  one\n  second\n  third\n" in rendered

    async def test_tool_output_keeps_the_last_lines(self, harness: TerminalHarness) -> None:
        lines = "".join(f"line {index}\n" for index in range(8))
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(tool_call={"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "seq"}}),
                ToolOutputEvent(tool_use_id="1", output=lines),
                ToolDoneEvent(tool_use_id="1", output=lines, is_error=False),
            ],
        )

        assert "  … +3 lines\n  line 3\n  line 4\n  line 5\n  line 6\n  line 7\n" in rendered
        assert "line 2" not in rendered

    async def test_tool_output_is_shown_as_a_terminal_would(self, harness: TerminalHarness) -> None:
        output = "\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\\n10%\r100%\n" + "x" * 200 + "\n"
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(
                    tool_call={
                        "type": "tool_use",
                        "id": "1",
                        "name": "bash",
                        "input": {"command": "make\n  && make test"},
                    }
                ),
                ToolOutputEvent(tool_use_id="1", output=output),
                ToolDoneEvent(tool_use_id="1", output=output, is_error=False),
            ],
        )

        # One header line, escapes and overwritten progress removed, long lines cut to the width.
        assert f"{TOOL_MARKER} Bash  make && make test\n  link\n  100%\n  {'x' * 77}…\n" in rendered

    async def test_tool_header_fits_one_line_and_keeps_the_suffix(self, harness: TerminalHarness) -> None:
        path = "src/" + "deep/" * 30 + "file.py"
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(
                    tool_call={"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "rg -n x " * 20}}
                ),
                ToolDoneEvent(tool_use_id="1", output="", is_error=False),
                ToolStartEvent(tool_call={"type": "tool_use", "id": "2", "name": "edit", "input": {"path": path}}),
                ToolDoneEvent(
                    tool_use_id="2", output="ok", is_error=False, metadata={"added_lines": 3, "removed_lines": 1}
                ),
            ],
        )

        bash, edit = [line for line in rendered.splitlines() if line.startswith(TOOL_MARKER)]
        assert bash.endswith("…")
        # A path is cut at the front so the file name and the suffix stay visible.
        assert edit.startswith(f"{TOOL_MARKER} Edit  …")
        assert edit.endswith("deep/file.py  +3 −1")

    async def test_tool_done_shows_final_status_after_live_output(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(
                    tool_call={"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "build"}}
                ),
                ToolOutputEvent(tool_use_id="1", output="started\n"),
                ToolDoneEvent(
                    tool_use_id="1",
                    output="started\n\n[Output truncated: Showing the last 50KB of output. "
                    + "Full output: /tmp/bash.log.]\n\n[Command timed out after 1s]",
                    is_error=True,
                ),
            ],
        )

        assert "[Output truncated: Showing the last 50KB of output." in rendered
        assert "/tmp/bash.log.]" in rendered
        assert "Command timed out after 1s" in rendered

    async def test_failed_command_output_is_not_repeated_as_status(self, harness: TerminalHarness) -> None:
        output = "error: could not compile `x`\n"
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(
                    tool_call={"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "cargo build"}}
                ),
                ToolOutputEvent(tool_use_id="1", output=output),
                ToolDoneEvent(tool_use_id="1", output=output + "\n[exit code: 101]", is_error=True),
            ],
        )

        assert "  error: could not compile `x`\n  [exit code: 101]\n" in rendered
        assert rendered.count("could not compile") == 1

    async def test_consecutive_tools_stay_together_between_text(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                TextEvent(delta="Checking."),
                ToolStartEvent(
                    tool_call={
                        "type": "tool_use",
                        "id": "1",
                        "name": "read",
                        "input": {"path": "a.py", "offset": 10, "limit": 5},
                    }
                ),
                ToolDoneEvent(tool_use_id="1", output="x", is_error=False),
                ToolStartEvent(tool_call={"type": "tool_use", "id": "2", "name": "edit", "input": {"path": "b.py"}}),
                ToolDoneEvent(
                    tool_use_id="2",
                    output="Updated b.py",
                    is_error=False,
                    metadata={"added_lines": 3, "removed_lines": 1},
                ),
                TextEvent(delta="Done."),
            ],
        )

        assert f"Checking.\n\n{TOOL_MARKER} Read  a.py  :10-15\n{TOOL_MARKER} Edit  b.py  +3 −1\n\nDone.\n" in rendered

    async def test_parallel_tools_print_in_the_order_they_finish(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                ToolStartEvent(
                    tool_call={"type": "tool_use", "id": "1", "name": "webfetch", "input": {"url": "https://a.test"}}
                ),
                ToolStartEvent(tool_call={"type": "tool_use", "id": "2", "name": "read", "input": {"path": "b.py"}}),
                ToolDoneEvent(tool_use_id="2", output="x", is_error=False),
                ToolDoneEvent(tool_use_id="1", output="error: timed out", is_error=True),
            ],
        )

        assert f"{TOOL_MARKER} Read  b.py\n{TOOL_MARKER} WebFetch  https://a.test\n  error: timed out\n" in rendered

    async def test_agent_call_shows_its_stats_and_reply_and_counts_its_cost(self, harness: TerminalHarness) -> None:
        reply = "\n".join(f"line {index}" for index in range(7))
        _, rendered = await _render_turn(
            harness,
            [
                _agent_start("1", "Find the config loader"),
                ToolOutputEvent(tool_use_id="1", output="Read config.py\n"),
                ToolDoneEvent(
                    tool_use_id="1",
                    output=f'{reply}\n\n[Agent 1 finished: 12 tool calls, 45s. Continue it with resume="1".]',
                    is_error=False,
                    metadata=_agent_metadata(tool_calls=12, duration_ms=45_210, cost=0.03),
                ),
                _agent_start("2", "Scan the tests", background=True),
                ToolDoneEvent(
                    tool_use_id="2",
                    output="Started agent in background: Scan the tests\nAgent id: 2",
                    is_error=False,
                    metadata={"agent": "general", "session_id": "2", "background": True},
                ),
                UsageEvent(context_tokens=None, turn_cost={"total": 0.01}, turn_usage={}, turn_duration_ms=None),
            ],
            session_cost_base=0.40,
        )

        # The reply tail replaces the summary lines; the notice for the model is left out.
        assert (
            f"{TOOL_MARKER} Agent  Find the config loader  12 tools · 45s · $0.03\n"
            "  … +2 lines\n  line 2\n  line 3\n  line 4\n  line 5\n  line 6\n"
            f"{TOOL_MARKER} Agent  Scan the tests  background\n"
        ) in rendered
        assert "Read config.py" not in rendered
        assert "[Agent" not in rendered
        # The session total includes the blocking child's spend.
        assert "m · $0.44" in rendered

    async def test_stopped_agent_call_shows_its_stats_reply_and_error(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                _agent_start("1", "Find the config loader"),
                ToolDoneEvent(
                    tool_use_id="1",
                    output="partial answer\n\nerror: cancelled by the user after 2 tool calls",
                    is_error=True,
                    metadata=_agent_metadata(status="cancelled", tool_calls=2, duration_ms=3000, cost=0.01),
                ),
            ],
        )

        assert (
            f"{TOOL_MARKER} Agent  Find the config loader  2 tools · 3s · $0.01\n"
            "  partial answer\n  error: cancelled by the user after 2 tool calls\n"
        ) in rendered

    async def test_failed_agent_call_shows_the_reply_and_its_error_without_stats(
        self, harness: TerminalHarness
    ) -> None:
        _, rendered = await _render_turn(
            harness,
            [
                _agent_start("1", "Find the config loader"),
                ToolDoneEvent(
                    tool_use_id="1",
                    output="partial answer\n\nerror: provider down",
                    is_error=True,
                    metadata=_agent_metadata(status="failed", tool_calls=2, duration_ms=3000, cost=0.01),
                ),
            ],
        )

        assert f"{TOOL_MARKER} Agent  Find the config loader\n  partial answer\n  error: provider down\n" in rendered

    async def test_running_agent_shows_its_summary_lines_in_the_tail(self, harness: TerminalHarness) -> None:
        renderer = TurnRenderer(harness.terminal, model="m", context_window=None)
        summary = "".join(f"Read file{index}.py\n" for index in range(7))
        agent = _EventAgent(
            [
                _agent_start("1", "Find the config loader"),
                ToolOutputEvent(tool_use_id="1", output=summary),
                ToolDoneEvent(tool_use_id="1", output="done", is_error=False, metadata=_agent_metadata()),
            ],
            delay=0.1,
        )

        async def main() -> None:
            await renderer.render(cast(Any, agent), "hi")

        await harness.terminal.run(main)

        rendered = harness.text()
        assert "Agent  Find the config loader" in rendered
        assert "… +2 lines" in rendered
        assert "Read file6.py" in rendered
        assert "Read file1.py" not in rendered

    async def test_finish_prints_context_and_session_cost(self, harness: TerminalHarness) -> None:
        _, rendered = await _render_turn(
            harness,
            [UsageEvent(context_tokens=34_210, turn_cost={"total": 0.02}, turn_usage={}, turn_duration_ms=None)],
            model="gpt-5.5",
            context_window=128_000,
            session_cost_base=0.40,
        )

        assert "gpt-5.5 · 34,210 tokens (27%) · $0.42" in rendered

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
        usage = UsageEvent(
            context_tokens=None,
            turn_usage={},
            turn_cost={"total": turn_cost} if turn_cost is not None else None,
            turn_duration_ms=None,
        )
        _, rendered = await _render_turn(
            harness,
            [usage],
            context_window=1_000,
            session_cost_base=session_cost_base,
        )

        if expected is None:
            assert "$" not in rendered
        else:
            assert f"${expected:.2f}" in rendered

    async def test_user_message_closes_the_segment_and_echoes_the_steer(self, harness: TerminalHarness) -> None:
        steer: ConversationMessage = {
            "role": "user",
            "content": [{"type": "text", "text": "use sqlite"}, {"type": "text", "text": "keep the tests"}],
            "meta": {"steer": True},
        }
        _, rendered = await _render_turn(
            harness,
            [
                TextEvent(delta="first segment"),
                UsageEvent(context_tokens=10, turn_cost={"total": 0.02}, turn_usage={}, turn_duration_ms=None),
                UserMessageEvent(message=steer),
                TextEvent(delta="second segment"),
                UsageEvent(context_tokens=20, turn_cost={"total": 0.03}, turn_usage={}, turn_duration_ms=None),
            ],
            session_cost_base=0.40,
        )

        # Each segment ends with its stats line; the session cost counts every segment once.
        assert (
            "first segment\n\nm · 10 tokens · $0.42\n\n❯ use sqlite\n\n  keep the tests  steer\n\nsecond segment\n\nm · 20 tokens · $0.45\n"
            in rendered
        )

    async def test_cancelled_event_is_a_muted_stop_not_an_error(self, harness: TerminalHarness) -> None:
        ended, rendered = await _render_turn(harness, [TextEvent(delta="partial"), CancelledEvent()])

        assert isinstance(ended, CancelledEvent)
        assert "partial\n\ncancelled\n" in rendered
        assert ERROR_MARKER not in rendered

    async def test_error_event_is_printed_and_fails_the_turn(self, harness: TerminalHarness) -> None:
        ended, rendered = await _render_turn(harness, [ErrorEvent(message="provider error")])

        assert isinstance(ended, ErrorEvent)
        assert f"{ERROR_MARKER} provider error" in rendered

    async def test_renders_inside_a_running_terminal(self, harness: TerminalHarness) -> None:
        renderer = TurnRenderer(harness.terminal, model="m", context_window=None)
        results: list[CancelledEvent | ErrorEvent | None] = []

        async def main() -> None:
            agent = _EventAgent(
                [
                    *_text_events("first block\n\nsecond block\n"),
                    UsageEvent(context_tokens=5, turn_usage={}, turn_cost=None, turn_duration_ms=None),
                ]
            )
            results.append(await renderer.render(cast(Any, agent), "hi"))

        await harness.terminal.run(main)

        rendered = harness.text()
        assert results == [None]
        assert "first block\n" in rendered
        assert "second block\n" in rendered
        assert "m · 5 tokens" in rendered


def _job_block(output: str, *, exit_code: int = 1) -> TextBlock:
    return text_block(
        f"Background bash finished (pid 1, exit code {exit_code}): pytest -q\nLog: /x.log\n\n{output}",
        meta={"job": {"tool_use_id": "toolu_1", "name": "bash", "label": "pytest -q", "exit_code": exit_code}},
    )


def _plain(renderables: list[Any]) -> str:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=80)
    for renderable in renderables:
        console.print(renderable)
    return re.sub(r" +\n", "\n", output.getvalue())


def _job_card(exit_code: int) -> str:
    return f"{TOOL_MARKER} Background finished  pytest -q  exit {exit_code}"


class TestJobLines:
    def test_failed_job_shows_the_output_tail_and_notices_under_an_exit_header(self) -> None:
        output = "\n".join(f"line {index}" for index in range(8)) + "\n\n[Output truncated: see /x.log]"

        rendered = _plain(job_lines(_job_block(output, exit_code=2), 80))

        assert rendered == (
            f"{_job_card(2)}\n  … +3 lines\n  line 3\n  line 4\n  line 5\n  line 6\n  line 7\n"
            "  [Output truncated: see /x.log]\n"
        )

    def test_successful_job_with_empty_output_is_only_the_header(self) -> None:
        assert _plain(job_lines(_job_block("(empty)", exit_code=0), 80)) == f"{_job_card(0)}\n"

    def test_long_label_is_shortened_to_keep_the_exit_code_on_the_line(self) -> None:
        block = _job_block("(empty)", exit_code=0)
        block["meta"]["job"]["label"] = "x" * 100

        [header] = job_lines(block, 60)

        assert header.cell_len <= 60
        assert header.plain.endswith("…  exit 0")


def _agent_job_block(reply: str, *, status: str = "completed") -> TextBlock:
    job = {
        "tool_use_id": "toolu_1",
        "name": "agent",
        "label": "Scan the tests",
        "session_id": "toolu_1",
        "status": status,
        "tool_calls": 12,
        "duration_ms": 45_210,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "cost": {"total": 0.03},
    }
    return text_block(f"Agent finished: Scan the tests\n\n{reply}", meta={"job": job})


class TestAgentJobLines:
    def test_finished_agent_shows_its_stats_and_the_reply_tail(self) -> None:
        reply = "\n".join(f"line {index}" for index in range(6))

        rendered = _plain(job_lines(_agent_job_block(reply), 80))

        assert rendered == (
            f"{TOOL_MARKER} Agent finished  Scan the tests  12 tools · 45s · $0.03\n"
            "  … +1 lines\n  line 1\n  line 2\n  line 3\n  line 4\n  line 5\n"
        )

    def test_stopped_agent_shows_its_error_under_a_failed_marker(self) -> None:
        [header, *body] = job_lines(_agent_job_block("partial\n\nerror: cancelled", status="cancelled"), 80)

        assert header.plain == f"{TOOL_MARKER} Agent stopped  Scan the tests  12 tools · 45s · $0.03"
        assert header.spans[0].style == ERROR
        assert [line.plain for line in body] == ["  partial", "  error: cancelled"]
        assert body[-1].style == ERROR

    def test_failed_agent_names_the_failure(self) -> None:
        [header, *_] = job_lines(_agent_job_block("error: provider down", status="failed"), 80)

        assert header.plain == f"{TOOL_MARKER} Agent failed  Scan the tests  12 tools · 45s · $0.03"
        assert header.spans[0].style == ERROR


def test_agent_transcript_prints_every_turn_between_rules() -> None:
    messages: list[ConversationMessage] = [
        {"role": "user", "content": [{"type": "text", "text": f"question {index}"}]} for index in range(4)
    ]
    messages.append({"role": "assistant", "content": [{"type": "text", "text": "the answer"}]})

    rendered = _plain(agent_transcript("Find the config loader", messages, width=80))

    assert rendered.startswith("── Agent: Find the config loader ──\n\n❯ question 0\n")
    assert rendered.endswith("❯ question 3\nthe answer\n\n── end ──\n")


def test_agent_transcript_shows_a_call_without_a_result_as_pending() -> None:
    messages: list[ConversationMessage] = [
        {"role": "user", "content": [{"type": "text", "text": "look around"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "c2", "name": "bash", "input": {"command": "rg foo"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "output": "x", "is_error": False}]},
    ]

    lines = agent_transcript("Look around", messages, width=80)

    rendered = _plain(lines)
    assert f"{TOOL_MARKER} Read  a.py\n{PENDING_MARKER} Bash  rg foo\n" in rendered
    pending = next(line for line in lines if isinstance(line, Text) and line.plain.startswith(PENDING_MARKER))
    assert pending.spans[0].style == MUTED


def test_empty_agent_transcript_says_so() -> None:
    assert _plain(agent_transcript("New task", [], width=80)) == "── Agent: New task ──\n\nno messages\n\n── end ──\n"


def test_history_preview_shows_background_results_as_tool_cards() -> None:
    job = _job_block("line a")
    messages: list[ConversationMessage] = [
        {"role": "user", "content": [{"type": "text", "text": "run the tests"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "started"}]},
        {"role": "user", "content": [job]},
        {"role": "assistant", "content": [{"type": "text", "text": "tests failed"}]},
        {"role": "user", "content": [job, {"type": "text", "text": "fix it"}], "meta": {"steer": True}},
    ]

    rendered = _plain(history_preview(messages, width=80))

    # The wake message is only the card; the steered one is the card, then the user's text.
    assert f"started\n\n{_job_card(1)}\n  line a\ntests failed\n\n" in rendered
    assert rendered.endswith(f"tests failed\n\n{_job_card(1)}\n  line a\n❯ fix it\n")
    assert rendered.count("❯") == 2


async def test_user_message_with_only_job_blocks_prints_the_card_without_a_steer_echo(
    harness: TerminalHarness,
) -> None:
    delivered: list[ConversationMessage] = []
    message: ConversationMessage = {"role": "user", "content": [_job_block("line a")], "meta": {"steer": True}}
    renderer = TurnRenderer(harness.terminal, model="m", context_window=None, on_user_message=delivered.append)

    await renderer.render(
        cast(Any, _EventAgent([TextEvent(delta="working"), UserMessageEvent(message=message)])),
        "hi",
    )

    rendered = harness.text()
    assert f"{_job_card(1)}\n  line a\n" in rendered
    assert "steer" not in rendered
    assert "❯" not in rendered
    assert delivered == [message]
