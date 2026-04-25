"""Tests for in-process run management."""

from __future__ import annotations

import asyncio

import pytest

from mycode.agent import Event
from mycode_cli.server.run_manager import ActiveRunError, RunManager

pytestmark = pytest.mark.asyncio


class BlockingAgent:
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


class SimpleAgent:
    def cancel(self) -> None:
        return None

    async def achat(self, user_input):
        text = user_input["content"][0]["text"] if isinstance(user_input, dict) else user_input
        yield Event("text", {"delta": f"reply:{text}"})


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
    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task


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

    state = await manager.get_run(first["id"])
    assert state is not None and state.task is not None
    first_agent.release.set()
    await state.task


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

    await manager.cancel_run(first["id"])

    first_state = await manager.get_run(first["id"])
    assert first_state is not None and first_state.task is not None
    await first_state.task

    updated_first = await manager.get_run(first["id"])
    updated_second = await manager.get_run(second["id"])
    assert updated_first is not None
    assert updated_first.status == "cancelled"
    assert updated_second is not None
    assert updated_second.status == "running"

    second_agent.release.set()
    assert updated_second.task is not None
    await updated_second.task


class ReviewAgent:
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
    for _ in range(200):
        state = await manager.get_run(run_id)
        assert state is not None
        for event in state.events:
            if event.get("type") == event_type:
                return event
        await asyncio.sleep(0.01)
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

    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task

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

    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task

    assert agent.decision == "deny"
    resolved_event = next(event for event in state.events if event["type"] == "permission_resolved")
    assert resolved_event["decision"] == "deny"


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

    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task

    assert agent.cancelled is True
    assert agent.decision == "deny"
    resolved_event = next(event for event in state.events if event["type"] == "permission_resolved")
    assert resolved_event["decision"] == "deny"
    assert state.pending_decisions == {}


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
    assert state is not None and state.task is not None
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

    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task

    finished = await manager.get_run(run["id"])
    assert finished is not None
    assert finished.status == "completed"
    assert await manager.snapshot_session("session-1") is None
