"""Tests for agent tool loops, persistence, and cancellation."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode.core.agent import Agent
from mycode.core.providers.base import ProviderStreamEvent
from mycode.core.tools import ToolExecutionResult, ToolExecutor, ToolSpec


class _FakeProviderAdapter:
    def __init__(self, turns: list[list[ProviderStreamEvent]]):
        self._turns = list(turns)

    async def stream_turn(self, request):
        events = self._turns.pop(0) if self._turns else []
        for event in events:
            yield event


class _CustomToolExecutor(ToolExecutor):
    def __init__(self, *, cwd: str, session_dir: Path) -> None:
        super().__init__(
            cwd=cwd,
            session_dir=session_dir,
            tools=(
                ToolSpec(
                    name="ping",
                    description="Echoes a short string.",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string", "description": "Text to echo."}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    method_name="ping",
                ),
            ),
        )

    def ping(self, *, text: str) -> ToolExecutionResult:
        return ToolExecutionResult(model_text=f"pong: {text}", display_text=f"pong: {text}")


class _SlowProviderAdapter:
    def __init__(self):
        self.closed = asyncio.Event()

    async def stream_turn(self, _request):
        try:
            yield ProviderStreamEvent("thinking_delta", {"text": "working"})
            await asyncio.sleep(10)
        finally:
            self.closed.set()


class TestAgentReasoningPersistence:
    @pytest.mark.asyncio
    async def test_achat_persists_reasoning_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persisted: list[dict] = []

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
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
            assert assistant_messages[0]["content"] == [
                {"type": "thinking", "text": "hidden "},
                {"type": "text", "text": "Visible answer"},
            ]

    @pytest.mark.asyncio
    async def test_achat_persists_tool_calls_from_messages_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persisted: list[dict] = []

            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
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
            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
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
            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
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


class TestCustomTools:
    @pytest.mark.asyncio
    async def test_agent_executes_custom_tool_executor_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=session_dir,
                tool_executor=_CustomToolExecutor(cwd=tmpdir, session_dir=session_dir),
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
                                            "name": "ping",
                                            "input": {"text": "hello"},
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
                events = [event async for event in agent.achat("hello")]

            assert [event.type for event in events] == ["tool_start", "tool_done"]
            assert events[1].data == {
                "tool_use_id": "call-1",
                "model_text": "pong: hello",
                "display_text": "pong: hello",
                "is_error": False,
            }


class TestAgentCancel:
    @pytest.mark.asyncio
    async def test_cancel_stops_inflight_provider_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(
                model="gpt-5.4",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
            )
            adapter = _SlowProviderAdapter()

            with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
                stream = agent.achat("hello")
                first_event = await anext(stream)
                assert first_event.type == "reasoning"
                assert first_event.data == {"delta": "working"}

                agent.cancel()
                remaining_events = [event async for event in stream]

            assert len(remaining_events) == 1
            assert remaining_events[0].type == "error"
            assert remaining_events[0].data == {"message": "cancelled"}
            assert adapter.closed.is_set()
