"""Tests for Agent construction, execution, persistence, and cancellation."""

import asyncio
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode import (
    Agent,
    ConversationMessage,
    Event,
    RunResult,
    SessionStore,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
    bash_tool,
    edit_tool,
    read_tool,
    text_block,
    tool,
    write_tool,
)
from mycode.providers.base import ProviderStreamEvent


class _FakeProviderAdapter:
    def __init__(self, turns: list[list[ProviderStreamEvent]]):
        self._turns = list(turns)
        self.requests = []
        self.message_snapshots = []

    async def stream_turn(self, request):
        self.requests.append(request)
        self.message_snapshots.append(deepcopy(request.messages))
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


def _assistant_turn(
    *blocks: dict[str, object],
    meta: dict[str, object] | None = None,
) -> list[ProviderStreamEvent]:
    message: dict[str, object] = {"role": "assistant", "content": list(blocks)}
    if meta is not None:
        message["meta"] = meta
    return [ProviderStreamEvent("message_done", {"message": message})]


def _text_turn(text: str = "done", *, meta: dict[str, object] | None = None) -> list[ProviderStreamEvent]:
    return _assistant_turn({"type": "text", "text": text}, meta=meta)


def _tool_turn(
    call_id: str,
    *,
    name: str = "read",
    tool_input: dict[str, object] | None = None,
) -> list[ProviderStreamEvent]:
    return _assistant_turn(
        {
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": {"path": "test.txt"} if tool_input is None else tool_input,
        }
    )


@tool
def read_back(context: ToolContext, path: str) -> str:
    """Read a file through the built-in read tool."""

    return context.read(path).output


@tool(streams_output=True)
async def read_back_async(context: ToolContext, path: str) -> str:
    """Read a file through the async built-in read tool."""

    output = (await context.aread(path)).output
    if context.emit:
        context.emit(output)
    return output


def _chat_events(events: list[Event]) -> list[Event]:
    """Drop the per-request usage events; usage flows have dedicated tests."""

    return [event for event in events if event.type != "usage"]


def _new_agent(tmp_path: Path, **overrides) -> Agent:
    overrides.setdefault("model", "gpt-5.5")
    overrides.setdefault("cwd", str(tmp_path))
    overrides.setdefault("session_dir", tmp_path)
    overrides.setdefault("session_id", "session")
    return Agent(**overrides)


class _SlowProviderAdapter:
    def __init__(self):
        self.closed = asyncio.Event()

    async def stream_turn(self, _request):
        try:
            yield ProviderStreamEvent("thinking_delta", {"text": "working"})
            await asyncio.sleep(10)
        finally:
            self.closed.set()


class _CancelledCompactAdapter:
    def __init__(self):
        self.requests = 0

    async def stream_turn(self, _request):
        self.requests += 1
        if self.requests == 1:
            yield _text_turn(meta={"usage": {"total_tokens": 90}})[0]
            return
        raise asyncio.CancelledError


def test_agent_forwards_runtime_configuration_to_provider(tmp_path: Path) -> None:
    default_adapter = _FakeProviderAdapter([_text_turn()])
    with patch("mycode.agent.get_provider_adapter", return_value=default_adapter):
        _new_agent(tmp_path).run("hello")

    configured_adapter = _FakeProviderAdapter([_text_turn()])
    configured_agent = _new_agent(
        tmp_path,
        session_id="configured-session",
        system="Use this exact system prompt.",
        reasoning_effort="vendor-specific",
        tools=[read_tool, write_tool, edit_tool, bash_tool],
    )
    with patch("mycode.agent.get_provider_adapter", return_value=configured_adapter):
        configured_agent.run("hello")

    assert default_adapter.requests[0].tools == []
    request = configured_adapter.requests[0]
    assert request.session_id == "configured-session"
    assert request.system == "Use this exact system prompt."
    assert request.reasoning_effort == "vendor-specific"
    assert {tool_definition["name"] for tool_definition in request.tools} == {
        "bash",
        "edit",
        "read",
        "write",
    }


def test_agent_requires_explicit_provider_for_unknown_models(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not infer provider"):
        _new_agent(tmp_path, model="not-a-real-model")


def test_agent_run_collects_stream_events(tmp_path: Path) -> None:
    adapter = _FakeProviderAdapter(
        [
            [
                ProviderStreamEvent("thinking_delta", {"text": "plan "}),
                ProviderStreamEvent("text_delta", {"text": "hello"}),
                *_assistant_turn(
                    {"type": "thinking", "text": "plan "},
                    {"type": "text", "text": "hello"},
                ),
            ]
        ]
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        result = _new_agent(tmp_path).run("hi")

    assert result.text == "hello"
    assert result.error is None
    events = _chat_events(result.events)
    assert [event.type for event in events] == ["reasoning", "reasoning_done", "text"]
    assert events[0].data == {"delta": "plan "}
    assert isinstance(events[1].data.get("duration_ms"), int)
    assert events[2].data == {"delta": "hello"}


def test_agent_run_rejects_non_user_messages(tmp_path: Path) -> None:
    adapter = _FakeProviderAdapter([_text_turn()])
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        result = _new_agent(tmp_path).run({"role": "assistant", "content": [text_block("bad")]})

    assert result.text == ""
    assert result.error == "user input must be a user message"
    assert [event.type for event in result.events] == ["error"]
    assert adapter.requests == []


@pytest.mark.asyncio
class TestAgentSessions:
    async def test_achat_persists_messages_to_the_session_store(self, tmp_path: Path) -> None:
        agent = _new_agent(tmp_path, session_id="s1")

        with patch("mycode.agent.get_provider_adapter", return_value=_FakeProviderAdapter([_text_turn("first reply")])):
            _ = [event async for event in agent.achat("first question")]

        loaded = await SessionStore(data_dir=tmp_path).load_session("s1")

        assert loaded is not None
        assert [message["role"] for message in loaded["messages"]] == ["user", "assistant"]

    async def test_achat_resumes_existing_session_history(self, tmp_path: Path) -> None:
        first = _new_agent(tmp_path, session_id="s2")
        with patch("mycode.agent.get_provider_adapter", return_value=_FakeProviderAdapter([_text_turn("first reply")])):
            _ = [event async for event in first.achat("first question")]

        second = _new_agent(tmp_path, session_id="s2")
        adapter = _FakeProviderAdapter([_text_turn("second reply")])

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            _ = [event async for event in second.achat("second question")]

        assert [message["content"][0]["text"] for message in adapter.message_snapshots[0]] == [
            "first question",
            "first reply",
            "second question",
        ]

    async def test_rejects_explicit_messages_for_existing_sessions(self, tmp_path: Path) -> None:
        agent = _new_agent(tmp_path, session_id="s3")
        with patch("mycode.agent.get_provider_adapter", return_value=_FakeProviderAdapter([_text_turn()])):
            _ = [event async for event in agent.achat("hi")]

        with pytest.raises(ValueError, match="already exists"):
            _new_agent(tmp_path, session_id="s3", messages=[])

        with pytest.raises(ValueError, match="already exists"):
            _new_agent(
                tmp_path,
                session_id="s3",
                messages=[{"role": "user", "content": [text_block("fake history")]}],
            )


@pytest.mark.asyncio
async def test_custom_tools_can_reuse_builtin_tools(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello from sdk\n", encoding="utf-8")
    adapter = _FakeProviderAdapter(
        [
            _tool_turn("call_1", name="read_back", tool_input={"path": "note.txt"}),
            _text_turn(),
        ]
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events(
            [event async for event in _new_agent(tmp_path, tools=[read_tool, read_back]).achat("Read note.txt")]
        )

    assert [event.type for event in events] == ["tool_start", "tool_done"]
    assert events[1].data["output"] == "hello from sdk"


@pytest.mark.asyncio
async def test_async_custom_tools_can_reuse_and_stream_builtin_output(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello from async sdk\n", encoding="utf-8")
    adapter = _FakeProviderAdapter(
        [
            _tool_turn("call_1", name="read_back_async", tool_input={"path": "note.txt"}),
            _text_turn(),
        ]
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events(
            [event async for event in _new_agent(tmp_path, tools=[read_tool, read_back_async]).achat("Read note.txt")]
        )

    assert [event.type for event in events] == ["tool_start", "tool_output", "tool_done"]
    assert events[1].data["output"] == "hello from async sdk"
    assert events[2].data["output"] == "hello from async sdk"


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
                        *_assistant_turn(
                            {"type": "thinking", "text": "hidden "},
                            {"type": "text", "text": "Visible answer"},
                        ),
                    ]
                ]
            )

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = _chat_events([event async for event in agent.achat("hello", on_persist=on_persist)])

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
                    _tool_turn("call-1"),
                    _text_turn(),
                ]
            )

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = _chat_events([event async for event in agent.achat("hello", on_persist=on_persist)])

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

            adapter = _FakeProviderAdapter([_tool_turn(f"call-{idx}") for idx in range(1, 22)] + [_text_turn()])

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = _chat_events([event async for event in agent.achat("hello")])

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
                    _tool_turn("call-1"),
                    _tool_turn("call-2"),
                ]
            )

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = _chat_events([event async for event in agent.achat("hello")])

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
                    _tool_turn("call-1", name="ping", tool_input={"text": "hello"}),
                    _text_turn(),
                ]
            )

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = _chat_events([event async for event in agent.achat("hello")])

            assert [event.type for event in events] == ["tool_start", "tool_done"]
            assert events[1].data == {
                "tool_use_id": "call-1",
                "output": "pong: hello",
                "is_error": False,
            }

    @pytest.mark.parametrize(
        ("streams_output", "event_types", "cancelled_output"),
        [
            (False, ["tool_start", "tool_done"], "error: cancelled"),
            (True, ["tool_start", "tool_output", "tool_done"], "working\nerror: cancelled"),
        ],
    )
    def test_cancel_stops_async_tool_from_another_thread(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        streams_output: bool,
        event_types: list[str],
        cancelled_output: str,
    ) -> None:
        monkeypatch.setenv("PYTHONASYNCIODEBUG", "1")
        started = threading.Event()
        cleaned_up = threading.Event()

        @tool(streams_output=streams_output)
        async def wait_forever(ctx: ToolContext) -> None:
            """Wait until the tool is cancelled."""

            if ctx.emit:
                ctx.emit("working")
                await asyncio.sleep(0)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        agent = Agent(
            model="gpt-5.5",
            provider="openai",
            cwd=str(tmp_path),
            tools=[wait_forever],
        )
        adapter = _FakeProviderAdapter(
            [
                _tool_turn("call-1", name="wait_forever", tool_input={}),
            ]
        )
        results: list[RunResult] = []
        run_thread = threading.Thread(target=lambda: results.append(agent.run("start")), daemon=True)

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            run_thread.start()
            assert started.wait(timeout=1)
            agent.cancel()
            run_thread.join(timeout=1)

        assert not run_thread.is_alive()
        assert cleaned_up.is_set()
        assert len(results) == 1
        events = _chat_events(results[0].events)
        assert [event.type for event in events] == event_types
        assert events[-1].data == {
            "tool_use_id": "call-1",
            "output": cancelled_output,
            "is_error": True,
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
                    _tool_turn("call-1", name="ping", tool_input={"text": "hello"}),
                ]
            )

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = _chat_events([event async for event in agent.achat("hello", on_persist=on_persist)])

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
    async def test_cancelled_automatic_compaction_emits_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(
                model="gpt-5.5",
                provider="openai",
                cwd=tmpdir,
                session_dir=Path(tmpdir),
                context_window=100,
                compact_threshold=0.8,
            )

            adapter = _CancelledCompactAdapter()

            with patch("mycode.agent.get_provider_adapter", return_value=adapter):
                events = [event async for event in agent.achat("hello")]

            assert adapter.requests == 2
            assert [event.type for event in events] == ["usage", "error"]
            assert events[-1].data == {"message": "cancelled"}

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

            [error_event] = remaining_events
            assert error_event.type == "error"
            assert error_event.data == {"message": "cancelled"}
            assert adapter.closed.is_set()
            assert agent.messages[-1]["role"] == "assistant"
            assert agent.messages[-1]["content"] == [
                {"type": "thinking", "text": "working", "meta": {"duration_ms": pytest.approx(0, abs=1000)}}
            ]
            assert agent.messages[-1]["meta"]["provider"] == "openai"
            assert agent.messages[-1]["meta"]["model"] == "gpt-5.5"


class TestTurnUsage:
    def test_all_unpriced_turn_has_no_cost_estimate(self, tmp_path: Path) -> None:
        adapter = _FakeProviderAdapter(
            [_text_turn(meta={"usage": {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}})]
        )
        agent = _new_agent(tmp_path)
        agent.model_pricing = None

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            result = agent.run("hello")

        assert result.usage == {
            "context_tokens": 100,
            "turn_usage": {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10},
            "turn_cost": None,
        }

    def test_usage_events_accumulate_across_tool_turns(self, tmp_path: Path) -> None:
        adapter = _FakeProviderAdapter(
            [
                _assistant_turn(
                    {"type": "tool_use", "id": "call-1", "name": "ping", "input": {"text": "hi"}},
                    meta={"usage": {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}},
                ),
                _text_turn(meta={"usage": {"total_tokens": 150, "input_tokens": 130, "output_tokens": 20}}),
            ]
        )
        agent = _new_agent(tmp_path, tools=[_PING_TOOL])
        agent.model_pricing = {"input": 1.0, "output": 2.0}

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            result = agent.run("hello")

        first, second = [event.data for event in result.events if event.type == "usage"]
        assert first == {
            "context_tokens": 100,
            "turn_usage": {
                "total_tokens": 100,
                "input_tokens": 90,
                "output_tokens": 10,
            },
            "turn_cost": pytest.approx({"input": 0.00009, "output": 0.00002, "total": 0.00011}),
        }
        assert second["context_tokens"] == 150
        assert second["turn_usage"]["total_tokens"] == 250
        assert second["turn_usage"]["input_tokens"] == 220
        assert second["turn_usage"]["output_tokens"] == 30
        assert second["turn_cost"] == pytest.approx({"input": 0.00022, "output": 0.00006, "total": 0.00028})
        assert agent.messages[1]["meta"]["cost"] == pytest.approx(
            {"input": 0.00009, "output": 0.00002, "total": 0.00011}
        )
        assert result.usage == second

    def test_request_without_usage_keeps_known_turn_totals(self, tmp_path: Path) -> None:
        adapter = _FakeProviderAdapter(
            [
                _assistant_turn(
                    {"type": "tool_use", "id": "call-1", "name": "ping", "input": {"text": "hi"}},
                    meta={"usage": {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}},
                ),
                _text_turn(),
            ]
        )
        agent = _new_agent(tmp_path, tools=[_PING_TOOL])
        agent.model_pricing = {"input": 1.0, "output": 2.0}

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            result = agent.run("hello")

        assert result.usage is not None
        assert result.usage["context_tokens"] is None
        assert result.usage["turn_usage"] == {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}
        assert result.usage["turn_cost"] == pytest.approx({"input": 0.00009, "output": 0.00002, "total": 0.00011})

    def test_total_only_request_downgrades_the_turn_cost(self, tmp_path: Path) -> None:
        adapter = _FakeProviderAdapter(
            [
                _assistant_turn(
                    {"type": "tool_use", "id": "call-1", "name": "ping", "input": {"text": "hi"}},
                    meta={"usage": {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}},
                ),
                _text_turn(
                    meta={
                        "usage": {"total_tokens": 150, "input_tokens": 130, "output_tokens": 20},
                        "cost": {"total": 0.02},
                    }
                ),
            ]
        )
        agent = _new_agent(tmp_path, tools=[_PING_TOOL])
        agent.model_pricing = {"input": 1.0, "output": 2.0}

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            result = agent.run("hello")

        assert result.usage is not None
        assert result.usage["turn_cost"] == pytest.approx({"total": 0.02011})

    def test_failed_followup_request_keeps_last_successful_usage(self, tmp_path: Path) -> None:
        class _FailingSecondRequestAdapter:
            def __init__(self) -> None:
                self.requests = 0

            async def stream_turn(self, _request):
                self.requests += 1
                if self.requests == 1:
                    yield _assistant_turn(
                        {"type": "tool_use", "id": "call-1", "name": "ping", "input": {"text": "hi"}},
                        meta={"usage": {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}},
                    )[0]
                    return
                raise ValueError("boom")

        agent = _new_agent(tmp_path, tools=[_PING_TOOL])
        agent.model_pricing = {"input": 1.0, "output": 2.0}

        with patch("mycode.agent.get_provider_adapter", return_value=_FailingSecondRequestAdapter()):
            result = agent.run("hello")

        assert result.error == "boom"
        assert len([event for event in result.events if event.type == "usage"]) == 1
        assert result.events[-1].type == "error"
        assert result.usage is not None
        assert result.usage["context_tokens"] == 100
        assert result.usage["turn_usage"] == {"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}
