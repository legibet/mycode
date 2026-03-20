"""Tests for in-process run management."""

from __future__ import annotations

import asyncio

import pytest

from mycode.core.agent import Event
from mycode.server.run_manager import ActiveRunError, RunManager


class BlockingAgent:
    def __init__(self) -> None:
        self.cancelled = False
        self.release = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()

    async def achat(self, user_input: str, *, on_persist=None):
        yield Event("text", {"delta": f"reply:{user_input}"})
        await self.release.wait()
        if self.cancelled:
            yield Event("error", {"message": "cancelled"})
            return
        if on_persist:
            await on_persist({"role": "assistant", "content": [{"type": "text", "text": f"reply:{user_input}"}]})


class SimpleAgent:
    def cancel(self) -> None:
        return None

    async def achat(self, user_input: str, *, on_persist=None):
        yield Event("text", {"delta": f"reply:{user_input}"})
        if on_persist:
            await on_persist({"role": "assistant", "content": [{"type": "text", "text": f"reply:{user_input}"}]})


@pytest.mark.asyncio
async def test_snapshot_includes_user_message_and_pending_events():
    manager = RunManager()
    agent = BlockingAgent()

    run = await manager.start_run(
        session_id="session-1",
        user_input="build feature",
        base_messages=[{"role": "assistant", "content": [{"type": "text", "text": "Earlier"}]}],
        agent=agent,
        on_persist=lambda message: asyncio.sleep(0),
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


@pytest.mark.asyncio
async def test_same_session_cannot_start_second_run():
    manager = RunManager()
    first_agent = BlockingAgent()

    run = await manager.start_run(
        session_id="session-1",
        user_input="first",
        base_messages=[],
        agent=first_agent,
        on_persist=lambda message: asyncio.sleep(0),
    )

    with pytest.raises(ActiveRunError):
        await manager.start_run(
            session_id="session-1",
            user_input="second",
            base_messages=[],
            agent=BlockingAgent(),
            on_persist=lambda message: asyncio.sleep(0),
        )

    first_agent.release.set()
    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task


@pytest.mark.asyncio
async def test_cancel_only_marks_target_run_cancelled():
    manager = RunManager()
    first_agent = BlockingAgent()
    second_agent = BlockingAgent()

    first = await manager.start_run(
        session_id="session-1",
        user_input="first",
        base_messages=[],
        agent=first_agent,
        on_persist=lambda message: asyncio.sleep(0),
    )
    second = await manager.start_run(
        session_id="session-2",
        user_input="second",
        base_messages=[],
        agent=second_agent,
        on_persist=lambda message: asyncio.sleep(0),
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


@pytest.mark.asyncio
async def test_finished_run_stays_available_for_reconnect_window():
    manager = RunManager()

    run = await manager.start_run(
        session_id="session-1",
        user_input="done",
        base_messages=[],
        agent=SimpleAgent(),
        on_persist=lambda message: asyncio.sleep(0),
    )

    state = await manager.get_run(run["id"])
    assert state is not None and state.task is not None
    await state.task

    finished = await manager.get_run(run["id"])
    assert finished is not None
    assert finished.status == "completed"
    assert await manager.snapshot_session("session-1") is None
