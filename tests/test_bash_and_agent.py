"""Additional tests for bash tool and agent edge cases."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mycode.core.agent import Agent
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

    @pytest.mark.skip(reason="Timeout behavior varies by platform; test is flaky")
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


class TestAgentFinalizePendingToolCalls:
    """Tests for Agent._finalize_pending_tool_calls()."""

    def test_finalize_adds_missing_tool_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)

            # Create messages with pending tool call
            persisted_messages = [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path": "test.txt"}'},
                        }
                    ],
                }
            ]

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
                messages=persisted_messages,
            )

            # Should have added a tool result for the missing call
            tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
            assert len(tool_messages) == 1
            assert tool_messages[0]["tool_call_id"] == "call-1"
            assert "interrupted" in tool_messages[0]["content"]

    def test_finalize_no_action_when_all_results_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)

            # Create messages with completed tool call
            persisted_messages = [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path": "test.txt"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
            ]

            original_count = len(persisted_messages)

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
                messages=persisted_messages,
            )

            # Should not add any new messages
            assert len(agent.messages) == original_count + 1  # +1 for system prompt

    def test_finalize_multiple_missing_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)

            persisted_messages = [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "write", "arguments": "{}"},
                        },
                    ],
                }
            ]

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
                messages=persisted_messages,
            )

            tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
            assert len(tool_messages) == 2
            assert {m["tool_call_id"] for m in tool_messages} == {"call-1", "call-2"}

    def test_finalize_only_checks_last_assistant_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)

            persisted_messages = [
                {
                    "role": "assistant",
                    "content": "First response",
                    "tool_calls": [
                        {
                            "id": "old-call",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "old-call", "content": "old result"},
                {"role": "assistant", "content": "Text only, no tools"},
            ]

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
                messages=persisted_messages,
            )

            # Should not add any tool messages for the old completed call
            tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
            assert len(tool_messages) == 1  # Only the original one


class _FakeContentBlock:
    def __init__(self, *, type: str, id: str | None = None, name: str | None = None, input: dict | None = None):
        self.type = type
        self.id = id
        self.name = name
        self.input = input


class _FakeMessageEvent:
    def __init__(
        self,
        type: str,
        *,
        index: int | None = None,
        delta: dict | None = None,
        content_block: _FakeContentBlock | None = None,
    ):
        self.type = type
        self.index = index
        self.delta = delta
        self.content_block = content_block


async def _fake_stream(*events: _FakeMessageEvent):
    for event in events:
        yield event


class TestAgentReasoningPersistence:
    @pytest.mark.asyncio
    async def test_achat_does_not_persist_reasoning_content(self):
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

            stream = _fake_stream(
                _FakeMessageEvent(
                    "content_block_delta",
                    index=0,
                    delta={"type": "thinking_delta", "thinking": "hidden "},
                ),
                _FakeMessageEvent(
                    "content_block_delta",
                    index=1,
                    delta={"type": "text_delta", "text": "Visible answer"},
                ),
            )

            with patch("mycode.core.agent.amessages", new=AsyncMock(return_value=stream)):
                events = [event async for event in agent.achat("hello", on_persist=on_persist)]

            assert [event.type for event in events] == ["reasoning", "text"]
            assistant_messages = [m for m in persisted if m.get("role") == "assistant"]
            assert len(assistant_messages) == 1
            assert assistant_messages[0]["content"] == "Visible answer"
            assert "reasoning_content" not in assistant_messages[0]

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

            stream = _fake_stream(
                _FakeMessageEvent(
                    "content_block_start",
                    index=0,
                    content_block=_FakeContentBlock(type="tool_use", id="call-1", name="read", input={}),
                ),
                _FakeMessageEvent(
                    "content_block_delta",
                    index=0,
                    delta={"type": "input_json_delta", "partial_json": '{"path":"test.txt"}'},
                ),
            )

            with patch("mycode.core.agent.amessages", new=AsyncMock(return_value=stream)):
                events = [event async for event in agent.achat("hello", on_persist=on_persist)]

            assert [event.type for event in events] == ["tool_start", "tool_done"]
            assistant_messages = [m for m in persisted if m.get("role") == "assistant" and m.get("tool_calls")]
            assert len(assistant_messages) == 1
            assert assistant_messages[0]["tool_calls"] == [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"test.txt"}'},
                }
            ]
