"""Tests for bash tool execution and cancellation."""

import asyncio
import tempfile
import threading
import time
from pathlib import Path

import pytest

from mycode.tools import (
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    bash_tool,
    cancel_all_tools,
    edit_tool,
    read_tool,
    write_tool,
)

_TOOLS = (read_tool, write_tool, edit_tool, bash_tool)


def _ctx(
    cwd: str,
    *,
    tool_output_dir: Path | None = None,
    tool_call_id: str | None = None,
    on_output=None,
) -> ToolContext:
    executor = ToolExecutor(_TOOLS)
    return ToolContext(
        executor=executor,
        cwd=cwd,
        tool_output_dir=tool_output_dir if tool_output_dir is not None else Path(cwd),
        tool_call_id=tool_call_id,
        emit=on_output,
    )


class TestBash:
    def test_bash_simple_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-1").bash("echo Hello")

            assert "Hello" in result.output
            assert result.is_error is False

    def test_bash_empty_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-2").bash("true")

            assert result.output == "(empty)"

    def test_bash_runs_in_shell_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-3").bash('printf "%s\n%s" "$PWD" "$HOME"')

            assert tmpdir in result.output
            assert str(Path.home()) in result.output

    def test_bash_does_not_wait_for_implicit_stdin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="stdin-devnull").bash(
                'python3 -c "import sys; data = sys.stdin.read(); print(repr(data))"',
                timeout=1,
            )

            assert result.output == "''"


class TestBashTimeout:
    def test_bash_timeout_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-timeout").bash(
                "printf 'started\\n'; sleep 5",
                timeout=1,
            )

            assert "started" in result.output
            assert "timed out after 1s" in result.output
            assert result.is_error is True

    def test_bash_zero_timeout_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-zero-timeout").bash("echo ok", timeout=0)

            assert "ok" in result.output
            assert "timeout" not in result.output.lower()


class TestBashTruncation:
    def test_bash_output_at_byte_limit_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="byte-limit").bash("python3 -c \"print('x' * 51200, end='')\"")

            assert len(result.output) == 51200
            assert "Output truncated:" not in result.output
            assert not (Path(tmpdir) / "bash-byte-limit.log").exists()

    def test_bash_large_output_truncated_by_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_output = Path(tmpdir) / "tool-output"
            result = _ctx(tmpdir, tool_output_dir=tool_output, tool_call_id="test-large").bash(
                'for i in $(seq 1 3000); do echo "line $i"; done'
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
            result = _ctx(tmpdir, tool_call_id="blank-lines").bash("python3 -c \"print('\\n' * 3000, end='')\"")

            assert "last 2000 of 3000 lines" in result.output
            assert (Path(tmpdir) / "bash-blank-lines.log").read_bytes() == b"\n" * 3000

    def test_bash_multiline_output_truncated_by_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="large-lines").bash(
                "python3 -c \"for i in range(100): print(f'{i:03}:' + 'x' * 1000)\""
            )

            assert "Showing the last 50KB of output" in result.output
            assert "final output line" not in result.output
            assert "Use read to inspect omitted output" in result.output
            assert "099:" in result.output
            assert (Path(tmpdir) / "bash-large-lines.log").stat().st_size > 50 * 1024

    def test_bash_long_final_line_truncated_by_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="long-line").bash("python3 -c \"print('界' * 40000, end='')\"")

            assert "Showing the last 50KB of the final output line" in result.output
            assert "Use Bash byte-range commands to inspect the complete line" in result.output
            assert "Full output:" in result.output
            assert "�" not in result.output
            assert (Path(tmpdir) / "bash-long-line.log").read_text() == "界" * 40000

    def test_bash_huge_line_before_short_tail_keeps_byte_tail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="huge-line").bash("python3 -c \"print('x' * 300000); print('END')\"")

            body, _, notice = result.output.partition("\n\n[Output truncated:")
            assert body.startswith("x")
            assert body.endswith("\nEND")
            assert len(body.encode("utf-8")) == 50 * 1024
            assert "Showing the last 50KB of output" in notice
            assert "final output line" not in notice


class TestBashExitCode:
    def test_bash_zero_exit_code_not_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="exit-0").bash("echo ok")

            assert "exit code" not in result.output

    def test_bash_exit_code_with_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="exit-output").bash("echo some output; exit 42")

            assert "some output" in result.output
            assert "[exit code: 42]" in result.output
            assert result.is_error is True


class TestBashCallback:
    def test_bash_callback_receives_appendable_deltas(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            received_chunks: list[str] = []

            ctx = _ctx(tmpdir, tool_call_id="test-callback", on_output=received_chunks.append)
            ctx.bash("echo line1 && echo line2")

            assert "".join(received_chunks) == "line1\nline2\n"


class TestAsyncBash:
    @pytest.mark.asyncio
    async def test_abash_streams_output_and_returns_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            received_chunks: list[str] = []
            ctx = _ctx(tmpdir, tool_call_id="async-output", on_output=received_chunks.append)

            result = await ctx.abash("printf 'first\\nsecond\\n'")

            assert result.output == "first\nsecond"
            assert "".join(received_chunks) == "first\nsecond\n"

    @pytest.mark.asyncio
    async def test_abash_cancellation_finishes_promptly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _ctx(tmpdir, tool_call_id="async-cancel")
            task = asyncio.create_task(ctx.abash("echo started; sleep 10", timeout=15))
            await asyncio.sleep(0.1)

            task.cancel()
            result = await asyncio.wait_for(task, timeout=2)

            assert result.is_error is True
            assert "started" in result.output
            assert result.output.endswith("error: cancelled")


class TestCancelAllTools:
    def test_cancel_all_tools_terminates_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _ctx(tmpdir, tool_call_id="long-running")
            result_holder: dict[str, ToolExecutionResult] = {}

            def run_bash():
                result_holder["result"] = ctx.bash("sleep 10", timeout=15)

            thread = threading.Thread(target=run_bash)
            thread.start()

            time.sleep(0.5)
            cancel_all_tools()
            thread.join(timeout=5)

            assert thread.is_alive() is False

    def test_cancel_active_only_terminates_own_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = _ctx(tmpdir, tool_output_dir=Path(tmpdir) / "session-1", tool_call_id="first")
            second = _ctx(tmpdir, tool_output_dir=Path(tmpdir) / "session-2", tool_call_id="second")

            first_result: dict[str, ToolExecutionResult] = {}
            second_result: dict[str, ToolExecutionResult] = {}

            def run_first() -> None:
                first_result["result"] = first.bash("sleep 10", timeout=15)

            def run_second() -> None:
                second_result["result"] = second.bash("sleep 10", timeout=15)

            first_thread = threading.Thread(target=run_first)
            second_thread = threading.Thread(target=run_second)
            first_thread.start()
            second_thread.start()

            time.sleep(0.5)
            first.executor.cancel_active()

            first_thread.join(timeout=5)
            assert first_thread.is_alive() is False
            assert first_result["result"].output == "(empty)\n\nerror: cancelled"
            assert first_result["result"].is_error is True

            time.sleep(0.5)
            assert second_thread.is_alive() is True

            second.executor.cancel_active()
            second_thread.join(timeout=5)
            assert second_thread.is_alive() is False
            assert second_result["result"].output == "(empty)\n\nerror: cancelled"
            assert second_result["result"].is_error is True
