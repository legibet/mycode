"""Tests for agent tool loops, persistence, and cancellation."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode.agent import Agent
from mycode.messages import ConversationMessage
from mycode.providers.base import ProviderStreamEvent
from mycode.tools import ToolContext, ToolExecutionResult, ToolSpec


class _FakeProviderAdapter:
    def __init__(self, turns: list[list[ProviderStreamEvent]]):
        self._turns = list(turns)

    async def stream_turn(self, request):
        events = self._turns.pop(0) if self._turns else []
        for event in events:
            yield event


def _ping_runner(_ctx: ToolContext, args: dict[str, object]) -> ToolExecutionResult:
    text = str(args.get("text") or "")
    return ToolExecutionResult(output=f"pong: {text}")


_PING_TOOL = ToolSpec(
    name="ping",
    description="Echoes a short string.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo."}},
        "required": ["text"],
        "additionalProperties": False,
    },
    runner=_ping_runner,
)


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
            persisted: list[ConversationMessage] = []

            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
            )

            async def on_persist(message: ConversationMessage) -> None:
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

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello", on_persist=on_persist)]

            assert [event.type for event in events] == ["reasoning", "reasoning_done", "text"]
            assert events[0].data == {"delta": "hidden "}
            duration_ms = events[1].data.get("duration_ms")
            assert isinstance(duration_ms, int)
            assert events[2].data == {"delta": "Visible answer"}
            assistant_messages = [m for m in persisted if m.get("role") == "assistant"]
            assert assistant_messages[0]["content"] == [
                {"type": "thinking", "text": "hidden ", "meta": {"duration_ms": duration_ms}},
                {"type": "text", "text": "Visible answer"},
            ]

    @pytest.mark.asyncio
    async def test_achat_persists_tool_calls_from_messages_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persisted: list[ConversationMessage] = []

            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
            )

            async def on_persist(message: ConversationMessage) -> None:
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

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
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
                model="gpt-5.5",
                provider="openai",
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

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello")]

            assert events[-1].type == "tool_done"
            assert all(event.data.get("message") != "max_turns reached" for event in events)

    @pytest.mark.asyncio
    async def test_achat_respects_explicit_turn_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(
                model="gpt-5.5",
                provider="openai",
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

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello")]

            assert events[-1].type == "error"
            assert events[-1].data == {"message": "max_turns reached"}


class TestCustomTools:
    @pytest.mark.asyncio
    async def test_agent_executes_custom_tool_executor_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=session_dir,
                tools=[_PING_TOOL],
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

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello")]

            assert [event.type for event in events] == ["tool_start", "tool_done"]
            assert events[1].data == {
                "tool_use_id": "call-1",
                "output": "pong: hello",
                "is_error": False,
            }

    @pytest.mark.asyncio
    async def test_cancel_after_assistant_persist_still_emits_tool_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
                tools=[_PING_TOOL],
            )

            async def on_persist(message: ConversationMessage) -> None:
                if message.get("role") == "assistant":
                    agent.cancel()

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
                    ]
                ]
            )

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello", on_persist=on_persist)]

            assert [event.type for event in events] == ["tool_start", "tool_done"]
            assert events[0].data == {
                "tool_call": {
                    "id": "call-1",
                    "name": "ping",
                    "input": {"text": "hello"},
                }
            }
            assert events[1].data == {
                "tool_use_id": "call-1",
                "output": "error: cancelled",
                "is_error": True,
            }


class TestAgentCancel:
    @pytest.mark.asyncio
    async def test_cancelled_compaction_propagates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
                context_window=100,
                compact_threshold=0.8,
            )

            adapter = _FakeProviderAdapter(
                [
                    [
                        ProviderStreamEvent(
                            "message_done",
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "done"}],
                                    "meta": {"total_tokens": 90},
                                }
                            },
                        )
                    ]
                ]
            )

            async def cancelled_compact(*_args, **_kwargs):
                raise asyncio.CancelledError
                yield

            with (
                patch("mycode.agent.get_provider_adapter", return_value=adapter),
                patch.object(agent, "_compact", cancelled_compact),
                pytest.raises(asyncio.CancelledError),
            ):
                [event async for event in agent.achat("hello")]

    @pytest.mark.asyncio
    async def test_cancel_stops_inflight_provider_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
            )
            adapter = _SlowProviderAdapter()

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
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
