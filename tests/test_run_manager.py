"""Tests for in-process run management."""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from typing import Any, override

import pytest
from conftest import FakeAgent

from mycode import Agent, tool
from mycode.agent import Event
from mycode.messages import ConversationMessage
from mycode.providers.base import ProviderStreamEvent
from mycode_cli.config import PermissionConfig, Settings
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, ToolReviewRequest, build_permission_hooks
from mycode_cli.server.run_manager import ActiveRunError, RunManager, RunState
from mycode_cli.sessions import SessionTotals

pytestmark = pytest.mark.asyncio


class ChatOnlyAgent(FakeAgent):
    """Base for chat fakes; compact runs never reach them."""

    async def acompact(self) -> ConversationMessage:
        raise NotImplementedError


class BlockingAgent(ChatOnlyAgent):
    def __init__(self) -> None:
        self.cancelled = False
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()

    async def achat(self, user_input, on_persist=None):
        text = user_input["content"][0]["text"] if isinstance(user_input, dict) else user_input
        yield Event("text", {"delta": f"reply:{text}"})
        await self.release.wait()
        if self.cancelled:
            yield Event("cancelled", {})


class SimpleAgent(ChatOnlyAgent):
    def cancel(self) -> None:
        return None

    async def achat(self, user_input, on_persist=None):
        text = user_input["content"][0]["text"] if isinstance(user_input, dict) else user_input
        yield Event("text", {"delta": f"reply:{text}"})


class RetryingAgent(SimpleAgent):
    @override
    async def achat(self, user_input, on_persist=None):
        yield Event("retry", {"attempt": 2, "max_attempts": 3})
        async for event in super().achat(user_input):
            yield event


class ToolOutputAgent(ChatOnlyAgent):
    def __init__(self, *, block_before_done: bool = False) -> None:
        self.block_before_done = block_before_done
        self.output_sent = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.release.set()

    async def achat(self, user_input, on_persist=None):
        del user_input
        yield Event("tool_start", {"tool_call": {"id": "call-1", "name": "bash", "input": {}}})
        yield Event("tool_output", {"tool_use_id": "call-1", "output": "a" * 8})
        yield Event("tool_output", {"tool_use_id": "call-1", "output": "b" * 8})
        self.output_sent.set()
        if self.block_before_done:
            await self.release.wait()
        yield Event("tool_done", {"tool_use_id": "call-1", "output": "final", "is_error": False})


async def _wait_for_run_task(manager: RunManager, run_id: str) -> RunState:
    state = await manager.get_run(run_id)
    assert state is not None
    assert state.task is not None
    await state.task
    return state


def _totals(cost: float | None, usage: dict[str, int] | None = None) -> SessionTotals:
    return SessionTotals(usage=usage or {}, cost={"total": cost} if cost is not None else None)


class UsageAgent(ChatOnlyAgent):
    def __init__(self, turn_cost: float | None) -> None:
        self.turn_cost = turn_cost

    def cancel(self) -> None:
        return None

    async def achat(self, user_input, on_persist=None):
        del user_input
        yield Event(
            "usage",
            {
                "context_tokens": 100,
                "turn_usage": {"input_tokens": 100, "output_tokens": 10},
                "turn_cost": {"total": self.turn_cost} if self.turn_cost is not None else None,
            },
        )


@pytest.mark.parametrize(
    ("session_cost_base", "turn_cost", "expected"),
    [
        (0.40, 0.01, 0.41),
        (0.40, None, 0.40),
        (None, 0.01, 0.01),
        (None, None, None),
    ],
)
async def test_usage_events_compose_known_session_totals(
    session_cost_base: float | None,
    turn_cost: float | None,
    expected: float | None,
) -> None:
    manager = RunManager()

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=UsageAgent(turn_cost),
        session_base=_totals(session_cost_base, usage={"input_tokens": 50}),
    )
    state = await _wait_for_run_task(manager, run["id"])

    usage_events = [event for event in state.events if event["type"] == "usage"]
    expected_cost = pytest.approx({"total": expected}) if expected is not None else None
    assert usage_events[0]["session_usage"] == {"input_tokens": 150, "output_tokens": 10}
    assert usage_events[0]["session_cost"] == expected_cost
    assert state.session_totals.cost == expected_cost
    assert usage_events[0]["model"] == "test-model"
    assert usage_events[0]["context_window"] == 1_000


# Chat runs and reconnect state


async def test_snapshot_includes_user_message_and_pending_events() -> None:
    manager = RunManager()
    agent = BlockingAgent()

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "build feature"}]},
        base_messages=[{"role": "assistant", "content": [{"type": "text", "text": "Earlier"}]}],
        agent=agent,
    )

    snapshot = None
    for _ in range(100):
        snapshot = await manager.snapshot_session("session-1")
        if snapshot and snapshot["pending_events"]:
            break
        await asyncio.sleep(0.01)

    assert snapshot is not None
    assert snapshot["run"]["id"] == run["id"]
    assert snapshot["messages"] == [
        {"role": "assistant", "content": [{"type": "text", "text": "Earlier"}]},
        {"role": "user", "content": [{"type": "text", "text": "build feature"}]},
    ]
    assert snapshot["pending_events"] == [{"seq": 1, "type": "text", "delta": "reply:build feature"}]

    agent.release.set()
    await _wait_for_run_task(manager, run["id"])


async def test_stream_events_respects_after_and_finishes() -> None:
    manager = RunManager()

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "done"}]},
        base_messages=[],
        agent=RetryingAgent(),
    )

    await _wait_for_run_task(manager, run["id"])

    events = [event async for event in manager.stream_events(run["id"], after=0)]
    assert events == [{"seq": 1, "type": "text", "delta": "reply:done"}]

    events_after_first = [event async for event in manager.stream_events(run["id"], after=1)]
    assert events_after_first == []


async def test_completed_tool_keeps_final_result_without_live_history() -> None:
    manager = RunManager()
    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "run"}]},
        base_messages=[],
        agent=ToolOutputAgent(),
    )

    state = await _wait_for_run_task(manager, run["id"])

    assert [(event["seq"], event["type"]) for event in state.events] == [
        (1, "tool_start"),
        (4, "tool_done"),
    ]


async def test_reconnect_buffer_reports_eviction_as_a_seq_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mycode_cli.server.run_manager.RUN_TOOL_OUTPUT_BUFFER_BYTES", 10)
    manager = RunManager()
    agent = ToolOutputAgent(block_before_done=True)
    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "run"}]},
        base_messages=[],
        agent=agent,
    )

    await asyncio.wait_for(agent.output_sent.wait(), timeout=1)
    snapshot = await manager.snapshot_session("session-1")

    assert snapshot is not None
    assert snapshot["pending_events"] == [{"seq": 3, "type": "tool_output", "tool_use_id": "call-1", "output": "b" * 8}]

    agent.release.set()
    await _wait_for_run_task(manager, run["id"])


class CommittingAgent(ChatOnlyAgent):
    """Chat fake that commits through ``on_persist`` like the SDK: each message
    before the events that follow it. It pauses after each of its two steps."""

    def __init__(self) -> None:
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()

    def cancel(self) -> None:
        return None

    async def _pause(self) -> None:
        self.paused.set()
        await self.resume.wait()
        self.resume.clear()

    async def achat(self, user_input, on_persist=None):
        assert on_persist is not None
        await on_persist(user_input)
        yield Event("text", {"delta": "a"})
        await on_persist(ASSISTANT_WITH_TOOL)
        yield Event("usage", {})
        yield Event("tool_start", {"tool_call": {"id": "call-1", "name": "bash", "input": {}}})
        yield Event("tool_done", {"tool_use_id": "call-1", "output": "ok", "is_error": False})
        await on_persist(TOOL_RESULT)
        yield Event("text", {"delta": "b"})
        await self._pause()
        await on_persist(STEER)
        yield Event("usage", {})
        yield Event("user_message", {"message": STEER})
        yield Event("text", {"delta": "c"})
        await self._pause()


ASSISTANT_WITH_TOOL: ConversationMessage = {
    "role": "assistant",
    "content": [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "call-1", "name": "bash", "input": {}}],
}
TOOL_RESULT: ConversationMessage = {
    "role": "user",
    "content": [{"type": "tool_result", "tool_use_id": "call-1", "output": "ok"}],
}
STEER: ConversationMessage = {
    "role": "user",
    "content": [{"type": "text", "text": "now"}],
    "meta": {"steer": True, "input_ids": ["s1"]},
}


async def test_snapshot_history_ends_at_the_last_committed_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mycode_cli.server.run_manager.RUN_EVENT_BUFFER_SIZE", 2)
    manager = RunManager()
    agent = CommittingAgent()
    user_message: ConversationMessage = {"role": "user", "content": [{"type": "text", "text": "run"}]}
    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message=user_message,
        base_messages=[{"role": "assistant", "content": [{"type": "text", "text": "Earlier"}]}],
        agent=agent,
    )

    async def snapshot_at_pause() -> dict[str, Any]:
        await asyncio.wait_for(agent.paused.wait(), 2)
        agent.paused.clear()
        snapshot = await manager.snapshot_session("session-1")
        assert snapshot is not None
        return snapshot

    # The events of the first step are evicted; the history covers them, and
    # only the events after the tool result replay onto it.
    snapshot = await snapshot_at_pause()
    assert snapshot["messages"][1:] == [user_message, ASSISTANT_WITH_TOOL, TOOL_RESULT]
    assert snapshot["pending_events"] == [{"seq": 5, "type": "text", "delta": "b"}]
    agent.resume.set()

    # A steer joins the history with its announcement, which the client
    # applies as the start of a segment, so the events replay from there.
    snapshot = await snapshot_at_pause()
    assert snapshot["messages"][4:] == [STEER]
    assert snapshot["pending_events"] == [{"seq": 8, "type": "text", "delta": "c"}]
    agent.resume.set()
    await _wait_for_run_task(manager, run["id"])


async def test_session_locks_are_reclaimed_after_normal_and_exceptional_exit() -> None:
    manager = RunManager()
    for index in range(100):
        async with manager.session_operation(str(index)):
            pass
    gc.collect()
    assert not manager._session_locks

    with pytest.raises(ValueError, match="operation failed"):
        async with manager.session_operation("failed"):
            raise ValueError("operation failed")
    gc.collect()
    assert not manager._session_locks


async def test_session_lock_preserves_waiters_across_cancellation_and_handoff() -> None:
    manager = RunManager()
    queued = {name: asyncio.Event() for name in ("cancelled", "waiting", "newcomer")}
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def operation(name: str) -> None:
        queued[name].set()
        async with manager.session_operation("shared"):
            order.append(name)
            entered.set()
            await release.wait()

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        async with manager.session_operation("shared"):
            cancelled = tasks.create_task(operation("cancelled"))
            tasks.create_task(operation("waiting"))
            await queued["cancelled"].wait()
            await queued["waiting"].wait()
            assert order == []
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

        # A new caller arrives before the queued waiter resumes.
        tasks.create_task(operation("newcomer"))
        await entered.wait()
        await queued["newcomer"].wait()
        assert order == ["waiting"]
        release.set()

    assert order == ["waiting", "newcomer"]


async def test_same_session_cannot_start_second_run() -> None:
    manager = RunManager()
    first_agent = BlockingAgent()

    first = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "first"}]},
        base_messages=[],
        agent=first_agent,
    )

    with pytest.raises(ActiveRunError):
        await manager.start_run(
            cwd="/work",
            session_id="session-1",
            user_message={"role": "user", "content": [{"type": "text", "text": "second"}]},
            base_messages=[],
            agent=BlockingAgent(),
        )

    first_agent.release.set()
    await _wait_for_run_task(manager, first["id"])


async def test_cancel_only_marks_target_run_cancelled() -> None:
    manager = RunManager()
    first_agent = BlockingAgent()
    second_agent = BlockingAgent()

    first = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "first"}]},
        base_messages=[],
        agent=first_agent,
    )
    second = await manager.start_run(
        cwd="/work",
        session_id="session-2",
        user_message={"role": "user", "content": [{"type": "text", "text": "second"}]},
        base_messages=[],
        agent=second_agent,
    )

    collected: list[dict[str, object]] = []
    got_text = asyncio.Event()

    async def collect() -> None:
        async for event in manager.stream_events(first["id"], after=0):
            collected.append(event)
            if event["type"] == "text":
                got_text.set()

    follower = asyncio.create_task(collect())
    await asyncio.wait_for(got_text.wait(), 2)

    cancelled = await manager.cancel_run(first["id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert "error" not in cancelled
    assert not await manager.has_active_run("session-1")
    await follower

    assert collected[0]["type"] == "text"
    assert collected[-1] == {"seq": 2, "type": "cancelled"}

    await _wait_for_run_task(manager, first["id"])

    updated_first = await manager.get_run(first["id"])
    updated_second = await manager.get_run(second["id"])
    assert updated_first is not None
    assert updated_first.status == "cancelled"
    assert updated_second is not None
    assert updated_second.status == "running"

    second_agent.release.set()
    assert updated_second.task is not None
    await updated_second.task


class CancelledAchatAgent(ChatOnlyAgent):
    """Agent whose achat raises ``CancelledError`` mid-stream.

    Mirrors the historical ``_compact`` cancellation path that used to leak
    past ``except Exception`` and leave ``_finish_run`` uncalled.
    """

    def cancel(self) -> None:
        return None

    async def achat(self, user_input, on_persist=None):
        del user_input
        yield Event("text", {"delta": "partial"})
        raise asyncio.CancelledError


async def test_cancelled_error_in_agent_still_finalizes_run() -> None:
    manager = RunManager()
    agent = CancelledAchatAgent()

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "go"}]},
        base_messages=[],
        agent=agent,
    )

    await _wait_for_run_task(manager, run["id"])

    final = await manager.get_run(run["id"])
    assert final is not None
    assert final.status == "cancelled"
    assert final.error is None
    assert [event["type"] for event in final.events] == ["text", "cancelled"]
    # Active-session lock must be released so the next /api/chat does not 409.
    assert not await manager.has_active_run("session-1")


class PersistFailAfterCancelAgent(ChatOnlyAgent):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.release.set()

    async def achat(self, user_input, on_persist=None):
        del user_input
        yield Event("text", {"delta": "partial"})
        self.started.set()
        await self.release.wait()
        raise OSError()


async def test_persist_failure_after_cancel_marks_run_failed() -> None:
    manager = RunManager()
    agent = PersistFailAfterCancelAgent()
    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=agent,
    )
    await asyncio.wait_for(agent.started.wait(), 2)

    finished = await manager.cancel_run(run["id"])
    assert finished is not None
    assert finished["status"] == "failed"

    state = await manager.get_run(run["id"])
    assert state is not None
    assert state.status == "failed"
    assert state.events[-1]["type"] == "error"
    assert not any(event["type"] == "cancelled" for event in state.events)
    assert not await manager.has_active_run("session-1")


# Permission decisions


class ReviewAgent(ChatOnlyAgent):
    """Fake agent that requests one permission decision mid-stream."""

    def __init__(self, manager: RunManager, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id
        self.cancelled = False
        self.decision: str | None = None

    def cancel(self) -> None:
        self.cancelled = True

    async def achat(self, user_input, on_persist=None):
        del user_input
        yield Event("text", {"delta": "before"})
        self.decision = await self.manager.request_decision(
            session_id=self.session_id,
            tool_call_id="call-1",
            tool_name="bash",
            preview="ls",
        )
        yield Event("text", {"delta": f"after:{self.decision}"})


async def _wait_for_event(manager: RunManager, run_id: str, event_type: str) -> dict[str, object]:
    try:
        async with asyncio.timeout(2):
            async for event in manager.stream_events(run_id, after=0):
                if event.get("type") == event_type:
                    return event
    except TimeoutError as exc:
        raise AssertionError(f"{event_type!r} never emitted") from exc

    raise AssertionError(f"{event_type!r} never emitted")


async def test_request_decision_allow_resumes_agent() -> None:
    manager = RunManager()
    agent = ReviewAgent(manager, "session-1")

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=agent,
    )

    request = await _wait_for_event(manager, run["id"], "permission_request")
    assert request["tool_use_id"] == "call-1"
    assert request["tool_name"] == "bash"
    assert request["preview"] == "ls"

    resolved = await manager.resolve_decision(run["id"], str(request["request_id"]), "allow")
    assert resolved is True

    state = await _wait_for_run_task(manager, run["id"])

    assert agent.decision == "allow"
    types = [event["type"] for event in state.events]
    assert types == ["text", "permission_request", "permission_resolved", "text"]
    resolved_event = next(event for event in state.events if event["type"] == "permission_resolved")
    assert resolved_event["decision"] == "allow"
    assert resolved_event["request_id"] == request["request_id"]
    assert state.pending_decisions == {}


async def test_request_decision_deny_returns_deny(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RunManager()
    calls: list[str] = []

    @tool
    def probe() -> str:
        """Must not execute after user denial."""
        calls.append("executed")
        return "unexpected"

    async def review(request: ToolReviewRequest):
        return await manager.request_decision(
            session_id="session-1",
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            preview=request.preview,
        )

    settings = Settings(
        providers={},
        port=8000,
        cwd=str(tmp_path),
        project=str(tmp_path),
        config_paths=[],
        permission=PermissionConfig(level="readonly", mode="ask"),
    )
    monkeypatch.setattr("mycode_cli.permissions.discover_skills", lambda _: [])
    agent = Agent(
        model="test",
        provider="openai",
        tools=[probe],
        max_turns=1,
        hooks=build_permission_hooks(settings, review=review),
    )

    class Adapter:
        async def stream_turn(self, _request):
            yield ProviderStreamEvent(
                "message_done",
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "call-1", "name": "probe", "input": {}}],
                        "meta": {"stop_reason": "tool_use"},
                    }
                },
            )

    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _: Adapter())

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=agent,
    )

    request = await _wait_for_event(manager, run["id"], "permission_request")
    assert await manager.resolve_decision(run["id"], str(request["request_id"]), "deny") is True

    state = await _wait_for_run_task(manager, run["id"])

    assert calls == []
    done = next(event for event in state.events if event["type"] == "tool_done")
    assert done["output"] == PERMISSION_DENIED_BY_USER_OUTPUT
    assert done["is_error"] is True
    resolved_event = next(event for event in state.events if event["type"] == "permission_resolved")
    assert resolved_event["decision"] == "deny"
    assert [event["type"] for event in state.events][-1] == "cancelled"
    assert state.status == "cancelled"
    assert state.error is None
    assert not await manager.has_active_run("session-1")


async def test_cancel_run_unblocks_pending_decision_as_deny() -> None:
    manager = RunManager()
    agent = ReviewAgent(manager, "session-1")

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=agent,
    )

    await _wait_for_event(manager, run["id"], "permission_request")
    await manager.cancel_run(run["id"])

    state = await _wait_for_run_task(manager, run["id"])

    assert agent.cancelled is True
    assert agent.decision == "deny"
    resolved_event = next(event for event in state.events if event["type"] == "permission_resolved")
    assert resolved_event["decision"] == "deny"
    assert [event["type"] for event in state.events][-1] == "cancelled"
    assert state.pending_decisions == {}


class CleanupAgent(ChatOnlyAgent):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_requested = asyncio.Event()
        self.cleaning_up = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.cleaned_up = False

    def cancel(self) -> None:
        self.cancel_requested.set()

    async def achat(self, user_input, on_persist=None):
        self.started.set()
        await self.cancel_requested.wait()
        self.cleaning_up.set()
        await self.release_cleanup.wait()
        self.cleaned_up = True
        yield Event("cancelled", {})


async def test_shutdown_preserves_tool_cleanup_after_cancel_request_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cleaning_up = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned_up = False

    @tool
    async def wait_tool() -> str:
        """Wait for cancellation and release resources."""
        nonlocal cleaned_up
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning_up.set()
            await release_cleanup.wait()
            cleaned_up = True
        return "cleaned up"

    class ToolAdapter:
        async def stream_turn(self, request):
            yield ProviderStreamEvent(
                "message_done",
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "call-1", "name": "wait_tool", "input": {}}],
                        "meta": {"stop_reason": "tool_use"},
                    }
                },
            )

    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _: ToolAdapter())
    manager = RunManager()
    agent = Agent(provider="anthropic", model="test-model", api_key="test-key", tools=[wait_tool])
    run = await manager.start_run(
        cwd="/work", session_id="s1", user_message={"role": "user"}, base_messages=[], agent=agent
    )
    await asyncio.wait_for(started.wait(), 2)
    request = asyncio.create_task(manager.cancel_run(run["id"]))
    await asyncio.wait_for(cleaning_up.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    state = await manager.get_run(run["id"])
    assert state is not None
    assert state.task is not None
    closing = asyncio.create_task(manager.aclose())
    try:
        await asyncio.sleep(0)
        assert not state.task.done()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(closing, 2)
    assert cleaned_up
    assert state.status == "cancelled"
    assert state.error is None
    types = [event["type"] for event in state.events]
    assert types[-1] == "cancelled"
    done = next(event for event in state.events if event["type"] == "tool_done")
    assert done["is_error"] is True
    assert done["output"] == "cleaned up"
    assert "error" not in types


async def test_close_cancels_all_runs_and_waits_for_cleanup() -> None:
    manager = RunManager()
    agent = CleanupAgent()
    review = ReviewAgent(manager, "review")
    run = await manager.start_run(
        cwd="/work", session_id="s1", user_message={"role": "user"}, base_messages=[], agent=agent
    )
    review_run = await manager.start_run(
        cwd="/work", session_id="review", user_message={"role": "user"}, base_messages=[], agent=review
    )
    await _wait_for_event(manager, review_run["id"], "permission_request")
    closing = asyncio.create_task(manager.aclose())
    await asyncio.wait_for(agent.cleaning_up.wait(), 2)
    try:
        await _wait_for_event(manager, review_run["id"], "permission_resolved")
        assert review.cancelled
        assert review.decision == "deny"
        assert not closing.done()
    finally:
        agent.release_cleanup.set()
        await asyncio.wait_for(closing, 2)
    assert agent.cleaned_up
    assert await manager.get_run(run["id"]) is None
    assert not await manager.has_active_run("s1")


async def test_close_does_not_enter_a_queued_agent_turn() -> None:
    manager = RunManager()
    agent = CleanupAgent()
    await manager.start_run(cwd="/work", session_id="s1", user_message={"role": "user"}, base_messages=[], agent=agent)
    async with asyncio.timeout(2):
        await manager.aclose()
    assert not agent.started.is_set()


async def test_resolve_decision_returns_false_for_unknown_request() -> None:
    manager = RunManager()
    agent = ReviewAgent(manager, "session-1")

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=agent,
    )

    await _wait_for_event(manager, run["id"], "permission_request")
    assert await manager.resolve_decision(run["id"], "missing-id", "allow") is False
    assert await manager.resolve_decision("missing-run", "any", "allow") is False

    # Resolve the real one so the run can finish cleanly.
    state = await manager.get_run(run["id"])
    assert state is not None
    assert state.task is not None
    request = next(event for event in state.events if event["type"] == "permission_request")
    await manager.resolve_decision(run["id"], str(request["request_id"]), "allow")
    await state.task


async def test_finished_run_stays_available_for_reconnect_window() -> None:
    manager = RunManager()

    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "done"}]},
        base_messages=[],
        agent=SimpleAgent(),
    )

    await _wait_for_run_task(manager, run["id"])

    finished = await manager.get_run(run["id"])
    assert finished is not None
    assert finished.status == "completed"
    assert await manager.snapshot_session("session-1") is None


# Compact runs


class CompactAgent(FakeAgent):
    """Fake compact agent that resolves once released, honoring cancel."""

    def __init__(self) -> None:
        self.cancelled = False
        self.compacted = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()

    async def achat(self, user_input, on_persist=None):
        del user_input
        raise NotImplementedError
        yield  # unreached; makes this an async generator

    async def acompact(self) -> ConversationMessage:
        self.started.set()
        await self.release.wait()
        if self.cancelled:
            raise asyncio.CancelledError
        self.compacted = True
        return {"role": "compact", "content": [{"type": "text", "text": "SUMMARY"}], "meta": {"cost": {"total": 0.1}}}


@pytest.mark.parametrize("session_cost_base", [None, 0.0, 0.4])
async def test_compact_run_snapshot_has_kind_and_no_user_message(session_cost_base: float | None) -> None:
    manager = RunManager()
    agent = CompactAgent()
    base = [{"role": "user", "content": [{"type": "text", "text": "earlier"}]}]
    completed: list[str] = []

    async def on_complete(session_id: str) -> None:
        completed.append(session_id)

    run = await manager.start_compact(
        cwd="/work",
        session_id="session-1",
        base_messages=base,
        agent=agent,
        session_base=_totals(session_cost_base),
        on_complete=on_complete,
    )
    assert run["kind"] == "compact"
    assert run["status"] == "running"

    snapshot = await manager.snapshot_session("session-1")
    assert snapshot is not None
    assert snapshot["run"]["kind"] == "compact"
    assert snapshot["messages"] == base
    assert snapshot["pending_events"] == []
    assert snapshot["totals"] == _totals(session_cost_base)

    agent.release.set()
    state = await _wait_for_run_task(manager, run["id"])

    assert agent.compacted is True
    assert state.status == "completed"
    assert state.events == [{"seq": 1, "type": "compact", "trigger": "manual"}]
    assert state.session_totals.cost == pytest.approx({"total": (session_cost_base or 0.0) + 0.1})
    assert completed == ["session-1"]
    assert not await manager.has_active_run("session-1")


async def test_compact_run_failure_emits_error_and_fails() -> None:
    manager = RunManager()
    completed: list[str] = []

    async def on_complete(session_id: str) -> None:
        completed.append(session_id)

    class FailingCompactAgent(CompactAgent):
        @override
        async def acompact(self) -> ConversationMessage:
            raise ValueError("nothing to compact")

    run = await manager.start_compact(
        cwd="/work",
        session_id="session-1",
        base_messages=[],
        agent=FailingCompactAgent(),
        session_base=_totals(0.4),
        on_complete=on_complete,
    )
    state = await _wait_for_run_task(manager, run["id"])

    assert state.status == "failed"
    assert state.session_totals == _totals(0.4)
    assert state.error == "nothing to compact"
    assert state.events == [{"seq": 1, "type": "error", "message": "nothing to compact"}]
    assert completed == []
    assert not await manager.has_active_run("session-1")


async def test_compact_run_cancellation_emits_cancelled_not_compact() -> None:
    manager = RunManager()
    agent = CompactAgent()

    run = await manager.start_compact(
        cwd="/work", session_id="session-1", base_messages=[], agent=agent, session_base=_totals(0.4)
    )
    await asyncio.wait_for(agent.started.wait(), 2)
    cancelled = await manager.cancel_run(run["id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["kind"] == "compact"
    assert "error" not in cancelled

    state = await _wait_for_run_task(manager, run["id"])
    assert agent.compacted is False
    assert state.events == [{"seq": 1, "type": "cancelled"}]
    assert state.session_totals == _totals(0.4)
    assert state.error is None
    assert not await manager.has_active_run("session-1")


async def test_chat_and_compact_conflict_on_same_session() -> None:
    manager = RunManager()
    agent = CompactAgent()

    run = await manager.start_compact(cwd="/work", session_id="session-1", base_messages=[], agent=agent)

    with pytest.raises(ActiveRunError):
        await manager.start_run(
            cwd="/work",
            session_id="session-1",
            user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
            base_messages=[],
            agent=SimpleAgent(),
        )
    with pytest.raises(ActiveRunError):
        await manager.start_compact(cwd="/work", session_id="session-1", base_messages=[], agent=CompactAgent())

    agent.release.set()
    await _wait_for_run_task(manager, run["id"])


# Steers and queue


def _input(text: str, input_id: str) -> ConversationMessage:
    return {"role": "user", "content": [{"type": "text", "text": text}], "meta": {"input_id": input_id}}


class QueueAgent(ChatOnlyAgent):
    """Chat fake that accepts steers; its first turn waits for release."""

    def __init__(self) -> None:
        self.inputs: list[str | ConversationMessage] = []
        self.steers: list[ConversationMessage] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self) -> None:
        return None

    @override
    def steer(self, message: ConversationMessage) -> bool:
        self.steers.append(message)
        return True

    @override
    def pending_steers(self) -> list[ConversationMessage]:
        return list(self.steers)

    async def achat(self, user_input, on_persist=None):
        self.inputs.append(user_input)
        if len(self.inputs) == 1:
            self.started.set()
            await self.release.wait()
        yield Event("text", {"delta": "reply"})


async def _start_queue_run(manager: RunManager, agent: QueueAgent) -> RunState:
    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "first"}]},
        base_messages=[],
        agent=agent,
    )
    await asyncio.wait_for(agent.started.wait(), 2)
    state = await manager.get_run(run["id"])
    assert state is not None
    return state


async def test_queue_continues_the_run_on_the_same_agent_with_one_merged_message() -> None:
    manager = RunManager()
    agent = QueueAgent()
    state = await _start_queue_run(manager, agent)

    assert await manager.enqueue(state, _input("a", "q1"))
    assert await manager.enqueue(state, _input("b", "q2"))
    assert state.agent.steer(_input("now", "s1"))
    snapshot = await manager.snapshot_session("session-1")
    assert snapshot is not None
    assert snapshot["pending"] == {"steers": [_input("now", "s1")], "queue": [_input("a", "q1"), _input("b", "q2")]}

    agent.release.set()
    await _wait_for_run_task(manager, state.id)

    merged = {
        "role": "user",
        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        "meta": {"input_ids": ["q1", "q2"]},
    }
    assert agent.inputs[1] == merged
    assert [event["type"] for event in state.events] == ["text", "user_message", "text"]
    assert state.events[1]["message"] == merged
    assert state.status == "completed"
    assert state.queue == []
    assert not await manager.has_active_run("session-1")
    assert not await manager.enqueue(state, _input("late", "q3"))


async def test_message_queued_during_a_queued_turn_runs_as_a_third_turn() -> None:
    class HeldContinuationAgent(QueueAgent):
        def __init__(self) -> None:
            super().__init__()
            self.announced = asyncio.Event()
            self.release_continuation = asyncio.Event()

        @override
        async def achat(self, user_input, on_persist=None):
            async for event in super().achat(user_input):
                yield event
            if len(self.inputs) == 2:
                self.announced.set()
                await self.release_continuation.wait()

    manager = RunManager()
    agent = HeldContinuationAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))

    agent.release.set()
    await asyncio.wait_for(agent.announced.wait(), 2)
    assert [event["type"] for event in state.events] == ["text", "user_message", "text"]
    assert await manager.enqueue(state, _input("b", "q2"))

    agent.release_continuation.set()
    await _wait_for_run_task(manager, state.id)

    third = {"role": "user", "content": [{"type": "text", "text": "b"}], "meta": {"input_ids": ["q2"]}}
    assert agent.inputs[2:] == [third]
    assert [event["type"] for event in state.events] == ["text", "user_message", "text", "user_message", "text"]
    assert state.events[3]["message"] == third
    assert state.status == "completed"
    assert state.queue == []
    assert not await manager.has_active_run("session-1")


async def test_sdk_user_message_advances_the_session_base() -> None:
    class SteeredAgent(ChatOnlyAgent):
        def cancel(self) -> None:
            return None

        async def achat(self, user_input, on_persist=None):
            yield Event("usage", {"turn_usage": {"input_tokens": 100}, "turn_cost": {"total": 0.01}})
            # The SDK closes the steered segment by repeating its totals.
            yield Event("usage", {"turn_usage": {"input_tokens": 100}, "turn_cost": {"total": 0.01}})
            yield Event("user_message", {"message": _input("steer", "s1")})
            yield Event("usage", {"turn_usage": {"input_tokens": 30}, "turn_cost": {"total": 0.02}})

    manager = RunManager()
    run = await manager.start_run(
        cwd="/work",
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "first"}]},
        base_messages=[],
        agent=SteeredAgent(),
        session_base=_totals(0.4, usage={"input_tokens": 50}),
    )
    state = await _wait_for_run_task(manager, run["id"])

    usage = [event for event in state.events if event["type"] == "usage"]
    assert [event["session_usage"] for event in usage] == [
        {"input_tokens": 150},
        {"input_tokens": 150},
        {"input_tokens": 180},
    ]
    assert [event["session_cost"] for event in usage] == [
        pytest.approx({"total": 0.41}),
        pytest.approx({"total": 0.41}),
        pytest.approx({"total": 0.43}),
    ]


async def test_cancel_before_queue_continuation_drops_the_queue() -> None:
    class StopAwareAgent(QueueAgent):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_requested = asyncio.Event()

        @override
        def cancel(self) -> None:
            self.cancel_requested.set()

    manager = RunManager()
    agent = StopAwareAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))

    cancelling = asyncio.create_task(manager.cancel_run(state.id))
    await asyncio.wait_for(agent.cancel_requested.wait(), 2)
    assert not await manager.enqueue(state, _input("b", "q2"))
    # The SDK turn ends normally after the stop request landed.
    agent.release.set()
    finished = await asyncio.wait_for(cancelling, 2)

    assert finished is not None
    assert finished["status"] == "cancelled"
    assert len(agent.inputs) == 1
    assert [event["type"] for event in state.events] == ["text", "cancelled"]


async def test_failure_drops_the_queue() -> None:
    class FailingAgent(QueueAgent):
        @override
        async def achat(self, user_input, on_persist=None):
            async for event in super().achat(user_input):
                yield event
            yield Event("error", {"message": "provider failed"})

    manager = RunManager()
    agent = FailingAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))

    agent.release.set()
    await _wait_for_run_task(manager, state.id)

    assert state.status == "failed"
    assert len(agent.inputs) == 1
    assert [event["type"] for event in state.events] == ["text", "error"]


async def test_snapshot_lists_the_queue_until_its_turn_is_announced() -> None:
    class SlowContinuationAgent(QueueAgent):
        def __init__(self) -> None:
            super().__init__()
            self.continued = asyncio.Event()
            self.release_continuation = asyncio.Event()

        @override
        async def achat(self, user_input, on_persist=None):
            if self.inputs:
                # The provider's first event can take a while after the commit.
                self.continued.set()
                await self.release_continuation.wait()
            async for event in super().achat(user_input):
                yield event

    manager = RunManager()
    agent = SlowContinuationAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))

    agent.release.set()
    await asyncio.wait_for(agent.continued.wait(), 2)
    # Committed by the continuation, not yet announced: still queued for a reconnect.
    snapshot = await manager.snapshot_session("session-1")
    assert snapshot is not None
    assert snapshot["pending"]["queue"] == [_input("a", "q1")]
    assert [event["type"] for event in snapshot["pending_events"]] == ["text"]
    assert await manager.remove_queued("session-1", "q1") is None

    agent.release_continuation.set()
    await _wait_for_run_task(manager, state.id)
    assert [event["type"] for event in state.events] == ["text", "user_message", "text"]
    assert state.delivering == []


async def test_failed_commit_of_the_queued_turn_is_not_announced() -> None:
    class CommitFailingAgent(QueueAgent):
        @override
        async def achat(self, user_input, on_persist=None):
            if self.inputs:
                self.inputs.append(user_input)
                # The real achat() fails this way when persisting its user message fails.
                raise OSError("disk full")
            async for event in super().achat(user_input):
                yield event

    manager = RunManager()
    agent = CommitFailingAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))

    agent.release.set()
    await _wait_for_run_task(manager, state.id)

    assert state.status == "failed"
    assert len(agent.inputs) == 2
    assert [event["type"] for event in state.events] == ["text", "error"]


async def test_steer_queued_moves_the_built_message_or_leaves_it_queued() -> None:
    class ClosingAgent(QueueAgent):
        def __init__(self) -> None:
            super().__init__()
            self.accepting_steers = True

        @override
        def steer(self, message: ConversationMessage) -> bool:
            return self.accepting_steers and super().steer(message)

    manager = RunManager()
    agent = ClosingAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))
    assert await manager.enqueue(state, _input("b", "q2"))

    assert await manager.steer_queued(state, "missing") is None
    assert await manager.steer_queued(state, "q1") is True
    assert agent.steers == [_input("a", "q1")]
    assert state.queue == [_input("b", "q2")]

    agent.accepting_steers = False
    assert await manager.steer_queued(state, "q2") is False
    assert state.queue == [_input("b", "q2")]

    agent.release.set()
    await _wait_for_run_task(manager, state.id)


async def test_remove_queued_drops_only_a_pending_message() -> None:
    manager = RunManager()
    agent = QueueAgent()
    state = await _start_queue_run(manager, agent)
    assert await manager.enqueue(state, _input("a", "q1"))

    assert await manager.remove_queued("session-1", "missing") is None
    removed = await manager.remove_queued("session-1", "q1")
    assert removed is not None
    assert removed["id"] == state.id
    assert state.queue == []

    agent.release.set()
    await _wait_for_run_task(manager, state.id)
    assert len(agent.inputs) == 1


async def test_compact_run_refuses_enqueue() -> None:
    manager = RunManager()
    agent = CompactAgent()
    run = await manager.start_compact(cwd="/work", session_id="session-1", base_messages=[], agent=agent)
    state = await manager.get_run(run["id"])
    assert state is not None

    assert not await manager.enqueue(state, _input("a", "q1"))

    agent.release.set()
    await _wait_for_run_task(manager, run["id"])
    assert state.queue == []
