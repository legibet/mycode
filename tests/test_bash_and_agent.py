"""Additional tests for bash tool and agent edge cases."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode.core.agent import Agent
from mycode.core.providers.base import ProviderStreamEvent
from mycode.core.tools import ToolExecutor, cancel_all_tools


class TestToolExecutorBash:
    """Tests for ToolExecutor.bash()."""

    def test_bash_simple_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-1", command="echo Hello")

            assert "Hello" in result
            assert "error" not in result.lower()

    def test_bash_multiple_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-2", command="echo 'line1\nline2'")

            assert "line1" in result
            assert "line2" in result

    def test_bash_stderr_included(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-3", command="echo error >&2")

            # stderr should be captured via stderr=STDOUT
            assert "error" in result

    def test_bash_empty_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-4", command="true")

            # Empty output should show "(empty)"
            assert "(empty)" in result or result == ""

    def test_bash_with_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-5", command="pwd")

            assert tmpdir in result

    def test_bash_with_pipes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-6", command="echo 'hello world' | wc -w")

            # Should count 2 words
            assert "2" in result

    def test_bash_with_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-7", command="echo $HOME")

            # HOME should be expanded
            assert result.strip() != "$HOME"


class TestBashTimeout:
    """Tests for bash timeout handling."""

    def test_bash_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            # Use a command that sleeps longer than timeout
            result = executor.bash(tool_call_id="test-timeout", command="sleep 5", timeout=1)

            assert "timeout" in result.lower()
            assert "error" in result.lower()

    def test_bash_quick_command_no_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-quick", command="echo fast", timeout=10)

            assert "fast" in result
            assert "timeout" not in result.lower()

    def test_bash_zero_timeout_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            result = executor.bash(tool_call_id="test-zero-timeout", command="echo ok", timeout=0)

            assert "ok" in result
            assert "timeout" not in result.lower()


class TestBashTruncation:
    """Tests for bash output truncation."""

    def test_bash_large_output_truncated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            # Generate output larger than default limits (2000 lines or 50KB)
            result = executor.bash(
                tool_call_id="test-large",
                command='for i in $(seq 1 3000); do echo "line $i"; done',
            )

            assert "truncated" in result.lower() or "showing" in result.lower()
            # Full output should be saved to file
            tool_output_dir = Path(tmpdir) / "tool-output"
            assert (tool_output_dir / "bash-test-large.log").exists()

    def test_bash_output_saved_for_large_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            executor.bash(
                tool_call_id="saved-output",
                command="seq 1 3000",
            )

            log_file = Path(tmpdir) / "tool-output" / "bash-saved-output.log"
            assert log_file.exists()
            # Log file should contain all lines
            content = log_file.read_text()
            assert "3000" in content


class TestBashCallback:
    """Tests for bash streaming callback."""

    def test_bash_callback_receives_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            received_lines = []

            def on_output(line: str) -> None:
                received_lines.append(line)

            executor.bash(
                tool_call_id="test-callback",
                command="echo line1 && echo line2",
                on_output=on_output,
            )

            # Callback should have received the lines
            assert len(received_lines) >= 2
            assert any("line1" in line for line in received_lines)


class TestCancelAllTools:
    """Tests for cancel_all_tools function."""

    def test_cancel_all_tools_terminates_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            # Start a long-running process
            import threading
            import time

            result_holder = {}

            def run_bash():
                result = executor.bash(
                    tool_call_id="long-running",
                    command="sleep 10",
                    timeout=15,
                )
                result_holder["result"] = result

            # Start bash in a thread
            thread = threading.Thread(target=run_bash)
            thread.start()

            # Give it time to start
            time.sleep(0.5)

            # Cancel all tools
            cancel_all_tools()

            # Wait for thread to finish
            thread.join(timeout=5)

            # Process should have been killed
            assert thread.is_alive() is False

    def test_cancel_active_only_terminates_own_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir) / "session-1")
            second = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir) / "session-2")

            import threading
            import time

            first_result: dict[str, str] = {}
            second_result: dict[str, str] = {}

            def run_first() -> None:
                first_result["result"] = first.bash(
                    tool_call_id="first",
                    command="sleep 10",
                    timeout=15,
                )

            def run_second() -> None:
                second_result["result"] = second.bash(
                    tool_call_id="second",
                    command="sleep 10",
                    timeout=15,
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


class _FakeProviderAdapter:
    def __init__(self, turns: list[list[ProviderStreamEvent]]):
        self._turns = list(turns)

    async def stream_turn(self, request):
        events = self._turns.pop(0) if self._turns else []
        for event in events:
            yield event


class TestAgentReasoningPersistence:
    @pytest.mark.asyncio
    async def test_achat_persists_reasoning_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            persisted: list[dict] = []

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
            )

            async def on_persist(message: dict) -> None:
                persisted.append(message)

            adapter = _FakeProviderAdapter(
                [
                    [
                        ProviderStreamEvent("thinking_delta", {"text": "hidden "}),
                        ProviderStreamEvent("text_delta", {"text": "Visible answer"}),
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {"type": "thinking", "text": "hidden "},
                                        {"type": "text", "text": "Visible answer"},
                                    ],
                                }
                            },
                        ),
                    ]
                ]
            )

            with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello", on_persist=on_persist)]

            assert [event.type for event in events] == ["reasoning", "text"]
            assert events[0].data == {"delta": "hidden "}
            assert events[1].data == {"delta": "Visible answer"}
            assistant_messages = [m for m in persisted if m.get("role") == "assistant"]
            assert len(assistant_messages) == 1
            assert assistant_messages[0]["content"] == [
                {"type": "thinking", "text": "hidden "},
                {"type": "text", "text": "Visible answer"},
            ]

    @pytest.mark.asyncio
    async def test_achat_persists_tool_calls_from_messages_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            persisted: list[dict] = []

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
            )

            async def on_persist(message: dict) -> None:
                persisted.append(message)

            adapter = _FakeProviderAdapter(
                [
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "call-1",
                                            "name": "read",
                                            "input": {"path": "test.txt"},
                                        }
                                    ],
                                }
                            },
                        )
                    ],
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "done"}],
                                }
                            },
                        )
                    ],
                ]
            )

            with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello", on_persist=on_persist)]

            assert [event.type for event in events] == ["tool_start", "tool_done"]
            assert events[0].data == {"tool_call": {"id": "call-1", "name": "read", "input": {"path": "test.txt"}}}
            assert events[1].data["tool_use_id"] == "call-1"
            assert events[1].data["is_error"] is True
            assistant_messages = [m for m in persisted if m.get("role") == "assistant"]
            assert len(assistant_messages) == 2
            assert assistant_messages[0]["content"] == [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "read",
                    "input": {"path": "test.txt"},
                }
            ]
            assert assistant_messages[1]["content"] == [{"type": "text", "text": "done"}]
            tool_results = [m for m in persisted if m.get("role") == "user" and m is not persisted[0]]
            assert len(tool_results) == 1
            assert tool_results[0]["content"][0]["type"] == "tool_result"
            assert tool_results[0]["content"][0]["tool_use_id"] == "call-1"


class TestAgentTurnLimits:
    @pytest.mark.asyncio
    async def test_achat_has_no_default_turn_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
            )

            adapter = _FakeProviderAdapter(
                [
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": f"call-{idx}",
                                            "name": "read",
                                            "input": {"path": "test.txt"},
                                        }
                                    ],
                                }
                            },
                        )
                    ]
                    for idx in range(1, 22)
                ]
                + [
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "done"}],
                                }
                            },
                        )
                    ]
                ]
            )

            with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello")]

            assert events[-1].type == "tool_done"
            assert all(event.data.get("message") != "max_turns reached" for event in events)

    @pytest.mark.asyncio
    async def test_achat_respects_explicit_turn_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
                max_turns=2,
            )

            adapter = _FakeProviderAdapter(
                [
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "call-1",
                                            "name": "read",
                                            "input": {"path": "test.txt"},
                                        }
                                    ],
                                }
                            },
                        )
                    ],
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "call-2",
                                            "name": "read",
                                            "input": {"path": "test.txt"},
                                        }
                                    ],
                                }
                            },
                        )
                    ],
                ]
            )

            with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello")]

            assert events[-1].type == "error"
            assert events[-1].data == {"message": "max_turns reached"}
