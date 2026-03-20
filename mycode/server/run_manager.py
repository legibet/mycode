"""In-process management for active web runs."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from mycode.core.agent import Event
from mycode.core.messages import ConversationMessage, user_text_message

RunStatus = Literal["running", "completed", "failed", "cancelled"]
FINISHED_RUN_TTL_SECONDS = 300


class ActiveRunError(RuntimeError):
    """Raised when a session already has a running task."""

    def __init__(self, run_id: str):
        super().__init__(run_id)
        self.run_id = run_id


class RunAgent(Protocol):
    def cancel(self) -> None: ...

    def achat(
        self,
        user_input: str,
        *,
        on_persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AsyncIterator[Event]: ...


@dataclass
class RunState:
    id: str
    session_id: str
    user_input: str
    base_messages: list[ConversationMessage]
    agent: RunAgent
    status: RunStatus = "running"
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    task: asyncio.Task[None] | None = None
    finished_at: float | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    def info(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "last_seq": self.next_seq - 1,
        }
        if self.error:
            payload["error"] = self.error
        return payload


class RunManager:
    """Track active session runs inside the current server process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_by_session: dict[str, RunState] = {}
        self._runs_by_id: dict[str, RunState] = {}

    async def start_run(
        self,
        *,
        session_id: str,
        user_input: str,
        base_messages: list[ConversationMessage],
        agent: RunAgent,
        on_persist: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        await self._prune_finished_runs()

        async with self._lock:
            existing = self._active_by_session.get(session_id)
            if existing:
                raise ActiveRunError(existing.id)

            state = RunState(
                id=uuid4().hex,
                session_id=session_id,
                user_input=user_input,
                base_messages=copy.deepcopy(base_messages),
                agent=agent,
            )
            state.task = asyncio.create_task(self._run(state, on_persist), name=f"mycode-run-{state.id}")
            self._active_by_session[session_id] = state
            self._runs_by_id[state.id] = state
            return state.info()

    async def get_run(self, run_id: str) -> RunState | None:
        await self._prune_finished_runs()
        async with self._lock:
            return self._runs_by_id.get(run_id)

    async def snapshot_session(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            state = self._active_by_session.get(session_id)

        if not state:
            return None

        async with state.condition:
            messages = copy.deepcopy(state.base_messages)
            messages.append(user_text_message(state.user_input))
            return {
                "run": state.info(),
                "messages": messages,
                "pending_events": copy.deepcopy(state.events),
            }

    async def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        state = await self.get_run(run_id)
        if not state:
            return None
        state.agent.cancel()
        return state.info()

    async def has_active_run(self, session_id: str) -> bool:
        async with self._lock:
            return session_id in self._active_by_session

    async def _run(self, state: RunState, on_persist: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        last_error: str | None = None

        async def persist(message: dict[str, Any]) -> None:
            await on_persist(message)

        try:
            stream = cast(AsyncIterator[Event], state.agent.achat(state.user_input, on_persist=persist))
            async for event in stream:
                if event.type == "error":
                    last_error = str(event.data.get("message") or "unknown error")
                await self._append_event(state, event)
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc)
            await self._append_event(state, Event("error", {"message": last_error}))

        if last_error == "cancelled":
            await self._finish_run(state, status="cancelled", error=last_error)
            return

        if last_error:
            await self._finish_run(state, status="failed", error=last_error)
            return

        await self._finish_run(state, status="completed")

    async def _append_event(self, state: RunState, event: Event) -> None:
        async with state.condition:
            payload = {"seq": state.next_seq, "type": event.type, **event.data}
            state.next_seq += 1
            state.events.append(payload)
            state.condition.notify_all()

    async def _finish_run(self, state: RunState, *, status: RunStatus, error: str | None = None) -> None:
        async with state.condition:
            state.status = status
            state.error = error
            state.finished_at = time.monotonic()
            state.condition.notify_all()

        async with self._lock:
            current = self._active_by_session.get(state.session_id)
            if current is state:
                self._active_by_session.pop(state.session_id, None)

    async def _prune_finished_runs(self) -> None:
        now = time.monotonic()
        async with self._lock:
            stale_run_ids = [
                run_id
                for run_id, state in self._runs_by_id.items()
                if state.finished_at is not None and (now - state.finished_at) >= FINISHED_RUN_TTL_SECONDS
            ]
            for run_id in stale_run_ids:
                self._runs_by_id.pop(run_id, None)
