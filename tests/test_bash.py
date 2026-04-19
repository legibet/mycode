"""Tests for bash tool execution and cancellation."""

import tempfile
import threading
import time
from pathlib import Path

from mycode.tools import (
    DEFAULT_TOOL_SPECS,
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    cancel_all_tools,
)


def _ctx(
    cwd: str,
    *,
    tool_output_dir: Path | None = None,
    tool_call_id: str | None = None,
    on_output=None,
) -> ToolContext:
    executor = ToolExecutor(DEFAULT_TOOL_SPECS)
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


class TestBashTimeout:
    def test_bash_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-timeout").bash("sleep 5", timeout=1)

            assert "timeout" in result.output.lower()
            assert result.is_error is True

    def test_bash_zero_timeout_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="test-zero-timeout").bash("echo ok", timeout=0)

            assert "ok" in result.output
            assert "timeout" not in result.output.lower()


class TestBashTruncation:
    def test_bash_large_output_truncated_by_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_output = Path(tmpdir) / "tool-output"
            result = _ctx(tmpdir, tool_output_dir=tool_output, tool_call_id="test-large").bash(
                'for i in $(seq 1 3000); do echo "line $i"; done'
            )

            assert "Truncated:" in result.output
            assert "of 3000 lines" in result.output
            assert "line 3000" in result.output
            assert "Full output:" in result.output
            assert (tool_output / "bash-test-large.log").exists()

    def test_bash_output_saved_for_large_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_output = Path(tmpdir) / "tool-output"
            _ctx(tmpdir, tool_output_dir=tool_output, tool_call_id="saved-output").bash("seq 1 3000")

            log_file = tool_output / "bash-saved-output.log"
            assert log_file.exists()
            assert "3000" in log_file.read_text()

    def test_bash_long_single_line_truncated_by_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="long-line").bash("python3 -c \"print('x' * 60000, end='')\"")

            assert "Truncated:" in result.output
            assert "KB limit" in result.output
            assert "Full output:" in result.output
            assert "0 lines" not in result.output
            assert "x" in result.output


class TestBashExitCode:
    def test_bash_nonzero_exit_code_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="exit-1").bash("exit 1")

            assert "[exit code: 1]" in result.output
            assert result.is_error is False

    def test_bash_zero_exit_code_not_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="exit-0").bash("echo ok")

            assert "exit code" not in result.output

    def test_bash_exit_code_with_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir, tool_call_id="exit-output").bash("echo some output; exit 42")

            assert "some output" in result.output
            assert "[exit code: 42]" in result.output
            assert result.is_error is False


class TestBashCallback:
    def test_bash_callback_receives_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            received_lines: list[str] = []

            ctx = _ctx(tmpdir, tool_call_id="test-callback", on_output=received_lines.append)
            ctx.bash("echo line1 && echo line2")

            assert len(received_lines) >= 2
            assert any("line1" in line for line in received_lines)


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

            time.sleep(0.5)
            assert second_thread.is_alive() is True

            second.executor.cancel_active()
            second_thread.join(timeout=5)
            assert second_thread.is_alive() is False
