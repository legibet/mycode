"""Tests for bash tool execution and cancellation."""

import tempfile
from pathlib import Path

from mycode.tools import ToolExecutionResult, ToolExecutor, cancel_all_tools


class TestToolExecutorBash:
    def test_bash_simple_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "echo Hello"}, tool_call_id="test-1")

            assert "Hello" in result.model_text
            assert result.is_error is False

    def test_bash_empty_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "true"}, tool_call_id="test-2")

            assert result.model_text == "(empty)"

    def test_bash_runs_in_shell_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": 'printf "%s\n%s" "$PWD" "$HOME"'}, tool_call_id="test-3")

            assert tmpdir in result.model_text
            assert str(Path.home()) in result.model_text


class TestBashTimeout:
    def test_bash_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "sleep 5", "timeout": 1}, tool_call_id="test-timeout")

            assert "timeout" in result.model_text.lower()
            assert result.is_error is True

    def test_bash_zero_timeout_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "echo ok", "timeout": 0}, tool_call_id="test-zero-timeout")

            assert "ok" in result.model_text
            assert "timeout" not in result.model_text.lower()


class TestBashTruncation:
    def test_bash_large_output_truncated_by_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_output = Path(tmpdir) / "tool-output"
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=tool_output)
            result = executor.execute(
                "bash", {"command": 'for i in $(seq 1 3000); do echo "line $i"; done'}, tool_call_id="test-large"
            )

            assert "Truncated:" in result.model_text
            assert "of 3000 lines" in result.model_text
            assert "line 3000" in result.model_text
            assert "Full output:" in result.model_text
            assert (tool_output / "bash-test-large.log").exists()

    def test_bash_output_saved_for_large_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_output = Path(tmpdir) / "tool-output"
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=tool_output)
            executor.execute("bash", {"command": "seq 1 3000"}, tool_call_id="saved-output")

            log_file = tool_output / "bash-saved-output.log"
            assert log_file.exists()
            assert "3000" in log_file.read_text()

    def test_bash_long_single_line_truncated_by_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute(
                "bash", {"command": "python3 -c \"print('x' * 60000, end='')\""}, tool_call_id="long-line"
            )

            assert "Truncated:" in result.model_text
            assert "KB limit" in result.model_text
            assert "Full output:" in result.model_text
            # Must not say "0 lines" — truncate_text slices the oversized line
            assert "0 lines" not in result.model_text
            assert "x" in result.model_text


class TestBashExitCode:
    def test_bash_nonzero_exit_code_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "exit 1"}, tool_call_id="exit-1")

            assert "[exit code: 1]" in result.model_text
            assert result.is_error is False

    def test_bash_zero_exit_code_not_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "echo ok"}, tool_call_id="exit-0")

            assert "exit code" not in result.model_text

    def test_bash_exit_code_with_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            result = executor.execute("bash", {"command": "echo some output; exit 42"}, tool_call_id="exit-output")

            assert "some output" in result.model_text
            assert "[exit code: 42]" in result.model_text
            assert result.is_error is False


class TestBashCallback:
    def test_bash_callback_receives_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))
            received_lines = []

            def on_output(line: str) -> None:
                received_lines.append(line)

            executor.execute(
                "bash",
                {"command": "echo line1 && echo line2"},
                tool_call_id="test-callback",
                on_output=on_output,
            )

            assert len(received_lines) >= 2
            assert any("line1" in line for line in received_lines)


class TestCancelAllTools:
    def test_cancel_all_tools_terminates_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir))

            import threading
            import time

            result_holder: dict[str, ToolExecutionResult] = {}

            def run_bash():
                result_holder["result"] = executor.execute(
                    "bash", {"command": "sleep 10", "timeout": 15}, tool_call_id="long-running"
                )

            thread = threading.Thread(target=run_bash)
            thread.start()

            time.sleep(0.5)
            cancel_all_tools()
            thread.join(timeout=5)

            assert thread.is_alive() is False

    def test_cancel_active_only_terminates_own_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir) / "session-1")
            second = ToolExecutor(cwd=tmpdir, tool_output_dir=Path(tmpdir) / "session-2")

            import threading
            import time

            first_result: dict[str, ToolExecutionResult] = {}
            second_result: dict[str, ToolExecutionResult] = {}

            def run_first() -> None:
                first_result["result"] = first.execute(
                    "bash", {"command": "sleep 10", "timeout": 15}, tool_call_id="first"
                )

            def run_second() -> None:
                second_result["result"] = second.execute(
                    "bash", {"command": "sleep 10", "timeout": 15}, tool_call_id="second"
                )

            first_thread = threading.Thread(target=run_first)
            second_thread = threading.Thread(target=run_second)
            first_thread.start()
            second_thread.start()

            time.sleep(0.5)
            first.cancel_active()

            first_thread.join(timeout=5)
            assert first_thread.is_alive() is False

            time.sleep(0.5)
            assert second_thread.is_alive() is True

            second.cancel_active()
            second_thread.join(timeout=5)
            assert second_thread.is_alive() is False
