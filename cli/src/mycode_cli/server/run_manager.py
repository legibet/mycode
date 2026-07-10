"""In-process management for active web runs."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from mycode.agent import Event
from mycode.messages import ConversationMessage
from mycode_cli.permissions import ToolReviewDecision

RunStatus = Literal["running", "completed", "failed", "cancelled"]
FINISHED_RUN_TTL_SECONDS = 300
RUN_EVENT_BUFFER_SIZE = 2000


class ActiveRunError(RuntimeError):
    """Raised when a session already has a running task."""

    def __init__(self, run_id: str):
        super().__init__(run_id)
        self.run_id = run_id


class RunAgent(Protocol):
    def cancel(self) -> None: ...

    def achat(self, user_input: str | ConversationMessage) -> AsyncIterator[Event]: ...


@dataclass
class RunState:
    """State for one in-process web run."""

    id: str
    session_id: str
    user_message: ConversationMessage
    base_messages: list[ConversationMessage]
    agent: RunAgent
    status: RunStatus = "running"
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    task: asyncio.Task[None] | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    pending_decisions: dict[str, asyncio.Future[ToolReviewDecision]] = field(default_factory=dict)

    def info(self) -> dict[str, Any]:
        """Return the public run payload."""

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
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def start_run(
        self,
        *,
        session_id: str,
        user_message: ConversationMessage,
        base_messages: list[ConversationMessage],
        agent: RunAgent,
    ) -> dict[str, Any]:
        await self._prune_finished_runs()

        async with self._lock:
            existing = self._active_by_session.get(session_id)
            if existing:
                raise ActiveRunError(existing.id)

            state = RunState(
                id=uuid4().hex,
                session_id=session_id,
                user_message=copy.deepcopy(user_message),
                base_messages=copy.deepcopy(base_messages),
                agent=agent,
            )
            state.task = asyncio.create_task(self._run(state), name=f"mycode-run-{state.id}")
            self._active_by_session[session_id] = state
            self._runs_by_id[state.id] = state
            return state.info()

    async def get_run(self, run_id: str) -> RunState | None:
        await self._prune_finished_runs()
        async with self._lock:
            return self._runs_by_id.get(run_id)

    async def snapshot_session(self, session_id: str) -> dict[str, Any] | None:
        """Return a reconnect snapshot for the active session."""

        async with self._lock:
            state = self._active_by_session.get(session_id)

        if not state:
            return None

        async with state.condition:
            messages = copy.deepcopy(state.base_messages)
            messages.append(copy.deepcopy(state.user_message))
            return {
                "run": state.info(),
                "messages": messages,
                "pending_events": list(state.events),
            }

    @asynccontextmanager
    async def session_operation(self, session_id: str) -> AsyncGenerator[None]:
        async with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock

        async with lock:
            yield

    async def active_run_info(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            state = self._active_by_session.get(session_id)
            return state.info() if state else None

    async def stream_events(self, run_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered and future events for a run after the given sequence."""

        state = await self.get_run(run_id)
        if not state:
            return

        last_seq = max(0, after)
        while True:
            async with state.condition:
                pending = [event for event in state.events if int(event.get("seq") or 0) > last_seq]
                finished = state.status != "running"

                if not pending and not finished:
                    # Wake on the next event or re-poll after the timeout; both just re-loop.
                    with suppress(TimeoutError):
                        await asyncio.wait_for(state.condition.wait(), timeout=0.5)
                    continue

            for payload in pending:
                yield payload
                last_seq = int(payload.get("seq") or last_seq)

            if finished:
                break

    async def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        state = await self.get_run(run_id)
        if not state:
            return None
        state.cancel_requested = True
        state.agent.cancel()
        for fut in state.pending_decisions.values():
            if not fut.done():
                fut.cancel()
        if state.task is not None:
            await state.task
        return state.info()

    async def has_active_run(self, session_id: str) -> bool:
        async with self._lock:
            return session_id in self._active_by_session

    async def request_decision(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        preview: str,
    ) -> ToolReviewDecision:
        """Emit permission_request, await the user's decision, then emit permission_resolved."""

        async with self._lock:
            state = self._active_by_session.get(session_id)
        if state is None:
            raise RuntimeError(f"no active run for session {session_id!r}")

        loop = asyncio.get_running_loop()
        request_id = uuid4().hex
        future: asyncio.Future[ToolReviewDecision] = loop.create_future()
        state.pending_decisions[request_id] = future

        await self._append_event(
            state,
            Event(
                "permission_request",
                {
                    "request_id": request_id,
                    "tool_use_id": tool_call_id,
                    "tool_name": tool_name,
                    "preview": preview,
                },
            ),
        )

        decision: ToolReviewDecision = "deny"
        try:
            decision = await future
            if decision == "deny":
                state.cancel_requested = True
                state.agent.cancel()
            return decision
        except asyncio.CancelledError:
            # Treat cancellation as deny so the agent loop unwinds via its own cancel check.
            return decision
        finally:
            state.pending_decisions.pop(request_id, None)
            await self._append_event(
                state,
                Event(
                    "permission_resolved",
                    {"request_id": request_id, "decision": decision},
                ),
            )

    async def resolve_decision(
        self,
        run_id: str,
        request_id: str,
        decision: ToolReviewDecision,
    ) -> bool:
        """Resolve a pending permission_request future. Returns whether resolved."""

        state = await self.get_run(run_id)
        if state is None:
            return False
        future = state.pending_decisions.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    async def _run(self, state: RunState) -> None:
        """Run the agent and store streamed events."""

        last_error: str | None = None

        try:
            async for event in state.agent.achat(state.user_message):
                if event.type == "error":
                    last_error = str(event.data.get("message") or "unknown error")
                await self._append_event(state, event)
        except asyncio.CancelledError:
            # BaseException, not caught by ``except Exception`` — without this
            # branch ``_finish_run`` never runs and the active-session lock
            # leaks (next /api/chat returns 409).
            last_error = "cancelled"
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc)
            await self._append_event(state, Event("error", {"message": last_error}))

        if state.cancel_requested or last_error == "cancelled":
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
            if len(state.events) > RUN_EVENT_BUFFER_SIZE:
                del state.events[: len(state.events) - RUN_EVENT_BUFFER_SIZE]
            state.condition.notify_all()

    async def _finish_run(self, state: RunState, *, status: RunStatus, error: str | None = None) -> None:
        for fut in state.pending_decisions.values():
            if not fut.done():
                fut.cancel()
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
        """Drop finished runs after the reconnect window."""

        now = time.monotonic()
        async with self._lock:
            stale_run_ids = [
                run_id
                for run_id, state in self._runs_by_id.items()
                if state.finished_at is not None and (now - state.finished_at) >= FINISHED_RUN_TTL_SECONDS
            ]
            for run_id in stale_run_ids:
                self._runs_by_id.pop(run_id, None)
