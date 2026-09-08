"""Tests for in-process run management."""

from __future__ import annotations

import asyncio
import gc
from typing import override

import pytest

from mycode import Agent, tool
from mycode.agent import Event
from mycode.messages import ConversationMessage
from mycode.providers.base import ProviderStreamEvent
from mycode_cli.server.run_manager import ActiveRunError, RunManager, RunState

pytestmark = pytest.mark.asyncio


class ChatOnlyAgent:
    """Base for chat fakes; compact runs never reach them."""

    model = "test-model"
    context_window = 1_000

    async def acompact(self) -> ConversationMessage:
        raise NotImplementedError


class BlockingAgent(ChatOnlyAgent):
    def __init__(self) -> None:
        self.cancelled = False
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()

    async def achat(self, user_input):
        text = user_input["content"][0]["text"] if isinstance(user_input, dict) else user_input
        yield Event("text", {"delta": f"reply:{text}"})
        await self.release.wait()
        if self.cancelled:
            yield Event("error", {"message": "cancelled"})


class SimpleAgent(ChatOnlyAgent):
    def cancel(self) -> None:
        return None

    async def achat(self, user_input):
        text = user_input["content"][0]["text"] if isinstance(user_input, dict) else user_input
        yield Event("text", {"delta": f"reply:{text}"})


class RetryingAgent(SimpleAgent):
    @override
    async def achat(self, user_input):
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

    async def achat(self, user_input):
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


class UsageAgent(ChatOnlyAgent):
    def __init__(self, turn_cost: float | None) -> None:
        self.turn_cost = turn_cost

    def cancel(self) -> None:
        return None

    async def achat(self, user_input):
        del user_input
        yield Event(
            "usage",
            {"context_tokens": 100, "turn_cost": {"total": self.turn_cost} if self.turn_cost is not None else None},
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
async def test_usage_events_compose_known_session_costs(
    session_cost_base: float | None,
    turn_cost: float | None,
    expected: float | None,
) -> None:
    manager = RunManager()

    run = await manager.start_run(
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=UsageAgent(turn_cost),
        session_cost_base=session_cost_base,
    )
    state = await _wait_for_run_task(manager, run["id"])

    usage_events = [event for event in state.events if event["type"] == "usage"]
    expected_cost = pytest.approx(expected) if expected is not None else None
    assert usage_events[0]["session_cost"] == expected_cost
    assert state.session_cost == expected_cost
    assert usage_events[0]["model"] == "test-model"
    assert usage_events[0]["context_window"] == 1_000


# Chat runs and reconnect state


async def test_snapshot_includes_user_message_and_pending_events() -> None:
    manager = RunManager()
    agent = BlockingAgent()

    run = await manager.start_run(
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
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "first"}]},
        base_messages=[],
        agent=first_agent,
    )

    with pytest.raises(ActiveRunError):
        await manager.start_run(
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
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "first"}]},
        base_messages=[],
        agent=first_agent,
    )
    second = await manager.start_run(
        session_id="session-2",
        user_message={"role": "user", "content": [{"type": "text", "text": "second"}]},
        base_messages=[],
        agent=second_agent,
    )

    cancelled = await manager.cancel_run(first["id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert not await manager.has_active_run("session-1")

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

    async def achat(self, user_input):
        del user_input
        yield Event("text", {"delta": "partial"})
        raise asyncio.CancelledError


async def test_cancelled_error_in_agent_still_finalizes_run() -> None:
    manager = RunManager()
    agent = CancelledAchatAgent()

    run = await manager.start_run(
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "go"}]},
        base_messages=[],
        agent=agent,
    )

    await _wait_for_run_task(manager, run["id"])

    final = await manager.get_run(run["id"])
    assert final is not None
    assert final.status == "cancelled"
    # Active-session lock must be released so the next /api/chat does not 409.
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

    async def achat(self, user_input):
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


async def test_request_decision_deny_returns_deny() -> None:
    manager = RunManager()
    agent = ReviewAgent(manager, "session-1")

    run = await manager.start_run(
        session_id="session-1",
        user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        base_messages=[],
        agent=agent,
    )

    request = await _wait_for_event(manager, run["id"], "permission_request")
    assert await manager.resolve_decision(run["id"], str(request["request_id"]), "deny") is True

    state = await _wait_for_run_task(manager, run["id"])

    assert agent.decision == "deny"
    assert agent.cancelled is True
    resolved_event = next(event for event in state.events if event["type"] == "permission_resolved")
    assert resolved_event["decision"] == "deny"
    assert state.status == "cancelled"
    assert not await manager.has_active_run("session-1")


async def test_cancel_run_unblocks_pending_decision_as_deny() -> None:
    manager = RunManager()
    agent = ReviewAgent(manager, "session-1")

    run = await manager.start_run(
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

    async def achat(self, user_input):
        self.started.set()
        await self.cancel_requested.wait()
        self.cleaning_up.set()
        await self.release_cleanup.wait()
        self.cleaned_up = True
        yield Event("error", {"message": "cancelled"})


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
    run = await manager.start_run(session_id="s1", user_message={"role": "user"}, base_messages=[], agent=agent)
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


async def test_close_cancels_all_runs_and_waits_for_cleanup() -> None:
    manager = RunManager()
    agent = CleanupAgent()
    review = ReviewAgent(manager, "review")
    run = await manager.start_run(session_id="s1", user_message={"role": "user"}, base_messages=[], agent=agent)
    review_run = await manager.start_run(
        session_id="review", user_message={"role": "user"}, base_messages=[], agent=review
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
    await manager.start_run(session_id="s1", user_message={"role": "user"}, base_messages=[], agent=agent)
    async with asyncio.timeout(2):
        await manager.aclose()
    assert not agent.started.is_set()


async def test_resolve_decision_returns_false_for_unknown_request() -> None:
    manager = RunManager()
    agent = ReviewAgent(manager, "session-1")

    run = await manager.start_run(
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


class CompactAgent:
    """Fake compact agent that resolves once released, honoring cancel."""

    model = "test-model"
    context_window = 1_000

    def __init__(self) -> None:
        self.cancelled = False
        self.compacted = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()

    async def achat(self, user_input):
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
        session_id="session-1",
        base_messages=base,
        agent=agent,
        session_cost_base=session_cost_base,
        on_complete=on_complete,
    )
    assert run["kind"] == "compact"
    assert run["status"] == "running"

    snapshot = await manager.snapshot_session("session-1")
    assert snapshot is not None
    assert snapshot["run"]["kind"] == "compact"
    assert snapshot["messages"] == base
    assert snapshot["pending_events"] == []
    assert snapshot["session_cost"] == session_cost_base

    agent.release.set()
    state = await _wait_for_run_task(manager, run["id"])

    assert agent.compacted is True
    assert state.status == "completed"
    assert state.events == [{"seq": 1, "type": "compact"}]
    assert state.session_cost == pytest.approx((session_cost_base or 0.0) + 0.1)
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
        session_id="session-1",
        base_messages=[],
        agent=FailingCompactAgent(),
        session_cost_base=0.4,
        on_complete=on_complete,
    )
    state = await _wait_for_run_task(manager, run["id"])

    assert state.status == "failed"
    assert state.session_cost == 0.4
    assert state.error == "nothing to compact"
    assert state.events == [{"seq": 1, "type": "error", "message": "nothing to compact"}]
    assert completed == []
    assert not await manager.has_active_run("session-1")


async def test_compact_run_cancellation_emits_no_compact_event() -> None:
    manager = RunManager()
    agent = CompactAgent()

    run = await manager.start_compact(session_id="session-1", base_messages=[], agent=agent, session_cost_base=0.4)
    await asyncio.wait_for(agent.started.wait(), 2)
    cancelled = await manager.cancel_run(run["id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["kind"] == "compact"

    state = await _wait_for_run_task(manager, run["id"])
    assert agent.compacted is False
    assert state.events == []
    assert state.session_cost == 0.4
    assert not await manager.has_active_run("session-1")


async def test_chat_and_compact_conflict_on_same_session() -> None:
    manager = RunManager()
    agent = CompactAgent()

    run = await manager.start_compact(session_id="session-1", base_messages=[], agent=agent)

    with pytest.raises(ActiveRunError):
        await manager.start_run(
            session_id="session-1",
            user_message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
            base_messages=[],
            agent=SimpleAgent(),
        )
    with pytest.raises(ActiveRunError):
        await manager.start_compact(session_id="session-1", base_messages=[], agent=CompactAgent())

    agent.release.set()
    await _wait_for_run_task(manager, run["id"])
