"""Tests for the CLI bash tool: execution, truncation, streaming, cancellation."""

import asyncio
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode import Agent, Event, RunResult
from mycode.providers.base import ProviderStreamEvent
from mycode.tools import ToolContext, ToolExecutor
from mycode_cli.tools import DEFAULT_TOOLS, bash_tool
from mycode_cli.workspace import CliDeps


def _ctx(
    cwd: str,
    *,
    tool_output_dir: Path | None = None,
    tool_call_id: str | None = None,
    on_output=None,
) -> ToolContext[CliDeps]:
    executor = ToolExecutor(DEFAULT_TOOLS)
    return ToolContext(
        executor=executor,
        deps=CliDeps(
            cwd=Path(cwd),
            tool_output_dir=tool_output_dir if tool_output_dir is not None else Path(cwd),
        ),
        tool_call_id=tool_call_id,
        emit=on_output,
    )


def _bash(ctx: ToolContext[CliDeps], command: str, *, timeout: int | None = None):
    return ctx.call("bash", {"command": command, "timeout": timeout})


class TestBash:
    def test_bash_simple_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="test-1"), "echo Hello")

            assert "Hello" in result.output
            assert result.is_error is False

    def test_bash_empty_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="test-2"), "true")

            assert result.output == "(empty)"

    def test_bash_runs_in_shell_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="test-3"), 'printf "%s\n%s" "$PWD" "$HOME"')

            assert tmpdir in result.output
            assert str(Path.home()) in result.output

    def test_bash_does_not_wait_for_implicit_stdin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(
                _ctx(tmpdir, tool_call_id="stdin-devnull"),
                'python3 -c "import sys; data = sys.stdin.read(); print(repr(data))"',
                timeout=1,
            )

            assert result.output == "''"


class TestBashTimeout:
    def test_bash_timeout_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(
                _ctx(tmpdir, tool_call_id="test-timeout"),
                "printf 'started\\n'; sleep 5",
                timeout=1,
            )

            assert "started" in result.output
            assert "timed out after 1s" in result.output
            assert result.is_error is True

    def test_bash_zero_timeout_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="test-zero-timeout"), "echo ok", timeout=0)

            assert "ok" in result.output
            assert "timeout" not in result.output.lower()


class TestBashTruncation:
    def test_bash_output_at_byte_limit_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="byte-limit"), "python3 -c \"print('x' * 51200, end='')\"")

            assert len(result.output) == 51200
            assert "Output truncated:" not in result.output
            assert not (Path(tmpdir) / "bash-byte-limit.log").exists()

    def test_bash_large_output_truncated_by_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_output = Path(tmpdir) / "tool-output"
            result = _bash(
                _ctx(tmpdir, tool_output_dir=tool_output, tool_call_id="test-large"),
                'for i in $(seq 1 3000); do echo "line $i"; done',
            )

            assert "Output truncated:" in result.output
            assert "last 2000 of 3000 lines" in result.output
            assert "Use read with offset to inspect earlier lines" in result.output
            assert "line 3000" in result.output
            assert "Full output:" in result.output
            log_file = tool_output / "bash-test-large.log"
            assert log_file.read_text().splitlines() == [f"line {i}" for i in range(1, 3001)]

    def test_bash_blank_lines_keep_accurate_line_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="blank-lines"), "python3 -c \"print('\\n' * 3000, end='')\"")

            assert "last 2000 of 3000 lines" in result.output
            assert (Path(tmpdir) / "bash-blank-lines.log").read_bytes() == b"\n" * 3000

    def test_bash_multiline_output_truncated_by_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(
                _ctx(tmpdir, tool_call_id="large-lines"),
                "python3 -c \"for i in range(100): print(f'{i:03}:' + 'x' * 1000)\"",
            )

            assert "Showing the last 50KB of output" in result.output
            assert "final output line" not in result.output
            assert "Use read to inspect omitted output" in result.output
            assert "099:" in result.output
            assert (Path(tmpdir) / "bash-large-lines.log").stat().st_size > 50 * 1024

    def test_bash_long_final_line_truncated_by_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="long-line"), "python3 -c \"print('界' * 40000, end='')\"")

            assert "Showing the last 50KB of the final output line" in result.output
            assert "Use Bash byte-range commands to inspect the complete line" in result.output
            assert "Full output:" in result.output
            assert "�" not in result.output
            assert (Path(tmpdir) / "bash-long-line.log").read_text() == "界" * 40000

    def test_bash_huge_line_before_short_tail_keeps_byte_tail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="huge-line"), "python3 -c \"print('x' * 300000); print('END')\"")

            body, _, notice = result.output.partition("\n\n[Output truncated:")
            assert body.startswith("x")
            assert body.endswith("\nEND")
            assert len(body.encode("utf-8")) == 50 * 1024
            assert "Showing the last 50KB of output" in notice
            assert "final output line" not in notice


class TestBashExitCode:
    def test_bash_zero_exit_code_not_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="exit-0"), "echo ok")

            assert "exit code" not in result.output

    def test_bash_exit_code_with_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _bash(_ctx(tmpdir, tool_call_id="exit-output"), "echo some output; exit 42")

            assert "some output" in result.output
            assert "[exit code: 42]" in result.output
            assert result.is_error is True


class TestBashCallback:
    def test_bash_callback_receives_appendable_deltas(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            received_chunks: list[str] = []

            ctx = _ctx(tmpdir, tool_call_id="test-callback", on_output=received_chunks.append)
            _bash(ctx, "echo line1 && echo line2")

            assert "".join(received_chunks) == "line1\nline2\n"


class TestAsyncBash:
    @pytest.mark.asyncio
    async def test_async_bash_streams_output_and_returns_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            received_chunks: list[str] = []
            ctx = _ctx(tmpdir, tool_call_id="async-output", on_output=received_chunks.append)

            result = await ctx.acall("bash", {"command": "printf 'first\\nsecond\\n'"})

            assert result.output == "first\nsecond"
            assert "".join(received_chunks) == "first\nsecond\n"

    @pytest.mark.asyncio
    async def test_async_bash_cancellation_finishes_promptly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _ctx(tmpdir, tool_call_id="async-cancel")
            task = asyncio.create_task(ctx.acall("bash", {"command": "echo started; sleep 10", "timeout": 15}))
            await asyncio.sleep(0.1)

            task.cancel()
            result = await asyncio.wait_for(task, timeout=2)

            assert result.is_error is True
            assert "started" in result.output
            assert result.output.endswith("error: cancelled")


# ---------------------------------------------------------------------------
# Agent integration: live output forwarding and cancellation
# ---------------------------------------------------------------------------


class _ScriptedProviderAdapter:
    """Yield one scripted assistant turn per provider request."""

    def __init__(self, turns: list[list[ProviderStreamEvent]]):
        self._turns = list(turns)

    async def stream_turn(self, _request):
        events = self._turns.pop(0) if self._turns else []
        for event in events:
            yield event


def _bash_turn(call_id: str, command: str) -> list[ProviderStreamEvent]:
    message = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": call_id, "name": "bash", "input": {"command": command}}],
        "meta": {"stop_reason": "tool_use"},
    }
    return [ProviderStreamEvent("message_done", {"message": message})]


def _text_turn() -> list[ProviderStreamEvent]:
    message = {"role": "assistant", "content": [{"type": "text", "text": "done"}]}
    return [ProviderStreamEvent("message_done", {"message": message})]


def _chat_events(events: list[Event]) -> list[Event]:
    """Drop the per-request usage events; usage flows have dedicated tests."""

    return [event for event in events if event.type != "usage"]


def _bash_agent(tmp_path: Path) -> Agent:
    return Agent(
        model="gpt-5.5",
        session_dir=tmp_path,
        session_id="session",
        tools=[bash_tool],
        deps=CliDeps.for_session(cwd=tmp_path, data_dir=tmp_path, session_id="session"),
    )


class TestAgentBash:
    @pytest.mark.asyncio
    async def test_agent_forwards_bash_live_output(self, tmp_path: Path) -> None:
        agent = _bash_agent(tmp_path)
        adapter = _ScriptedProviderAdapter(
            [
                _bash_turn("call-1", "printf 'one\\nsecond\\n'"),
                _text_turn(),
            ]
        )

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            events = _chat_events([event async for event in agent.achat("run bash")])

        tool_output_events = [event for event in events if event.type == "tool_output"]
        assert events[0].type == "tool_start"
        assert events[-1].type == "tool_done"
        assert all(event.type == "tool_output" for event in events[1:-1])
        assert tool_output_events
        assert "".join(event.data["output"] for event in tool_output_events) == "one\nsecond\n"
        assert events[-1].data["output"] == "one\nsecond"
        assert events[-1].data["is_error"] is False

    def test_cancel_stops_bash_from_another_thread(self, tmp_path: Path) -> None:
        marker = tmp_path / "bash-started"
        agent = _bash_agent(tmp_path)
        adapter = _ScriptedProviderAdapter(
            [
                _bash_turn(
                    "call-1",
                    (
                        'python3 -c "import pathlib,sys,time; '
                        "sys.stdout.write('x' * 60000); sys.stdout.flush(); "
                        f"pathlib.Path('{marker.name}').touch(); time.sleep(10)\""
                    ),
                )
            ]
        )
        results: list[RunResult] = []
        run_thread = threading.Thread(target=lambda: results.append(agent.run("run bash")), daemon=True)

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            run_thread.start()
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.01)
            assert marker.exists()
            agent.cancel()
            run_thread.join(timeout=3)

        assert not run_thread.is_alive()
        events = _chat_events(results[0].events)
        tool_done = [event for event in events if event.type == "tool_done"]
        assert len(tool_done) == 1
        result = tool_done[0].data
        assert result["tool_use_id"] == "call-1"
        assert result["is_error"] is True
        assert "Output truncated:" in result["output"]
        assert "error: cancelled" in result["output"]
        log_path = tmp_path / agent.session_id / "tool-output" / "bash-call-1.log"
        assert str(log_path) in result["output"]
        assert log_path.read_bytes() == b"x" * 60000
