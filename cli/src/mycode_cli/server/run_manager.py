"""In-process management for active web runs."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, Protocol, override
from uuid import uuid4
from weakref import WeakValueDictionary

from mycode.agent import Agent, PersistCallback
from mycode.events import (
    CancelledEvent,
    CompactEvent,
    ErrorEvent,
    Event,
    ToolDoneEvent,
    UsageEvent,
    UserMessageEvent,
)
from mycode.messages import ConversationMessage, merge_user_messages, user_text_message
from mycode_cli.background import BackgroundJobs, is_job_message
from mycode_cli.permissions import ToolReviewDecision
from mycode_cli.runtime import LocalAgentHost
from mycode_cli.server.schemas import RunKind, RunStatus, StreamEvent
from mycode_cli.sessions import SessionLock, SessionTotals, sum_session_totals
from mycode_cli.workspace import ChildOutcome, ChildTurn, OnChildComplete

logger = logging.getLogger(__name__)

RunCompletionCallback = Callable[[str], Awaitable[None]]
QueuedTurnCallback = Callable[[ConversationMessage], Awaitable[None]]
FINISHED_RUN_TTL_SECONDS = 300
RUN_EVENT_BUFFER_SIZE = 2000
RUN_TOOL_OUTPUT_BUFFER_BYTES = 1_000_000


def _tool_output_bytes(payload: dict[str, Any]) -> int:
    if payload.get("type") != "tool_output":
        return 0
    output = payload.get("output")
    return len(output.encode("utf-8")) if isinstance(output, str) else 0


class ActiveRunError(RuntimeError):
    """Raised when a session already has a running task."""

    def __init__(self, run_id: str):
        super().__init__(run_id)
        self.run_id = run_id


class RunAgent(Protocol):
    model: str
    context_window: int
    supports_image_input: bool
    supports_pdf_input: bool

    messages: list[ConversationMessage]

    def cancel(self) -> None: ...

    def steer(self, message: ConversationMessage) -> bool: ...

    def pending_steers(self) -> list[ConversationMessage]: ...

    def achat(
        self, user_input: str | ConversationMessage, *, on_persist: PersistCallback | None = None
    ) -> AsyncGenerator[Event, None]: ...

    def acompact(self) -> Awaitable[ConversationMessage]: ...


@dataclass
class RunState:
    """State for one in-process web run."""

    id: str
    session_id: str
    cwd: str
    base_messages: list[ConversationMessage]
    agent: RunAgent
    kind: RunKind = "chat"
    user_message: ConversationMessage | None = None
    # Started by the session's job registry to deliver background results.
    wake: bool = False
    # The session's cross-process claim, released with the active slot.
    lock: SessionLock | None = None
    # Queued messages the current turn continues from, until their merged
    # message is announced; snapshots list them as still queued.
    delivering: list[ConversationMessage] = field(default_factory=list)
    # Messages for the next turn of a chat run, each carrying ``meta.input_id``.
    queue: list[ConversationMessage] = field(default_factory=list)
    # Messages the run committed, and how far snapshots return them as history:
    # through the last committed user message, whose events end at
    # ``history_seq``. The events after it rebuild the rest, as on the live stream.
    committed: list[ConversationMessage] = field(default_factory=list)
    history_len: int = 0
    history_seq: int = 0
    # Pre-run history totals, combined with the cumulative turn totals on each update.
    session_base: SessionTotals = field(default_factory=SessionTotals)
    session_totals: SessionTotals = field(default_factory=SessionTotals)
    on_complete: RunCompletionCallback | None = None
    # Records a queued turn in the session catalog once the run takes it.
    on_queued_turn: QueuedTurnCallback | None = None
    # An agent run: called in the run's task with every SDK event the run
    # buffers, for the ``agent`` tool following the child turn ...
    tap: Callable[[Event], None] | None = None
    # ... and given the turn's outcome before the run is reported finished.
    on_child_complete: OnChildComplete | None = None
    status: RunStatus = "running"
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    tool_output_bytes: int = 0
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
            "kind": self.kind,
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
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._jobs: dict[str, BackgroundJobs] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def jobs_for(self, session_id: str) -> BackgroundJobs:
        """Return the session's background job registry, creating it on first use."""

        jobs = self._jobs.get(session_id)
        if jobs is None:
            jobs = self._jobs[session_id] = BackgroundJobs()
            jobs.on_change = lambda event_type, job: self._broadcast(
                {"type": event_type, "session_id": session_id, "job": job.info()}
            )
        return jobs

    def live_jobs(self, session_id: str) -> list[dict[str, Any]]:
        """Describe the session's running background jobs for clients."""

        jobs = self._jobs.get(session_id)
        return [job.info() for job in jobs.jobs] if jobs is not None else []

    def has_job(self, session_id: str, tool_use_id: str) -> bool:
        """Whether the session has the running background job ``tool_use_id``."""

        jobs = self._jobs.get(session_id)
        return jobs is not None and any(job.tool_use_id == tool_use_id for job in jobs.jobs)

    def kill_job(self, session_id: str, tool_use_id: str) -> bool:
        """Kill one running background job; ``False`` when the session has no such job."""

        jobs = self._jobs.get(session_id)
        return jobs is not None and jobs.kill(tool_use_id)

    async def close_jobs(self, session_id: str) -> None:
        """Kill the session's background jobs and forget its registry."""

        jobs = self._jobs.pop(session_id, None)
        if jobs is not None:
            await jobs.close()

    @asynccontextmanager
    async def subscribe(self) -> AsyncGenerator[asyncio.Queue[dict[str, Any]]]:
        """Receive run and background job lifecycle events for every session."""

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def _publish(self, event_type: str, state: RunState) -> None:
        self._broadcast({"type": event_type, "session_id": state.session_id, "run": state.info()})

    def _broadcast(self, payload: dict[str, Any]) -> None:
        for queue in self._subscribers:
            queue.put_nowait(payload)

    async def start_run(
        self,
        *,
        session_id: str,
        cwd: str,
        user_message: ConversationMessage,
        base_messages: list[ConversationMessage],
        agent: RunAgent,
        kind: Literal["chat", "agent"] = "chat",
        session_base: SessionTotals | None = None,
        wake: bool = False,
        lock: SessionLock | None = None,
        on_queued_turn: QueuedTurnCallback | None = None,
        tap: Callable[[Event], None] | None = None,
        on_child_complete: OnChildComplete | None = None,
    ) -> dict[str, Any]:
        """Start a run of ``agent.achat(user_message)``.

        A ``chat`` run is a session's turn and continues with its queue. An
        ``agent`` run is one subagent turn (``ServerAgentHost``), keyed
        ``<parent id>/<child id>``: the ``agent`` tool holds the child's lock,
        and the run takes no queue and has no jobs.
        """

        return await self._start(
            session_id=session_id,
            cwd=cwd,
            kind=kind,
            user_message=user_message,
            base_messages=base_messages,
            agent=agent,
            session_base=session_base,
            wake=wake,
            lock=lock,
            on_queued_turn=on_queued_turn,
            tap=tap,
            on_child_complete=on_child_complete,
        )

    async def start_compact(
        self,
        *,
        session_id: str,
        cwd: str,
        base_messages: list[ConversationMessage],
        agent: RunAgent,
        session_base: SessionTotals | None = None,
        on_complete: RunCompletionCallback | None = None,
        lock: SessionLock | None = None,
    ) -> dict[str, Any]:
        return await self._start(
            session_id=session_id,
            cwd=cwd,
            kind="compact",
            user_message=None,
            base_messages=base_messages,
            agent=agent,
            session_base=session_base,
            on_complete=on_complete,
            lock=lock,
        )

    async def _start(
        self,
        *,
        session_id: str,
        cwd: str,
        kind: RunKind,
        user_message: ConversationMessage | None,
        base_messages: list[ConversationMessage],
        agent: RunAgent,
        session_base: SessionTotals | None = None,
        on_complete: RunCompletionCallback | None = None,
        wake: bool = False,
        lock: SessionLock | None = None,
        on_queued_turn: QueuedTurnCallback | None = None,
        tap: Callable[[Event], None] | None = None,
        on_child_complete: OnChildComplete | None = None,
    ) -> dict[str, Any]:
        await self._prune_finished_runs()
        session_base = session_base or SessionTotals()

        async with self._lock:
            existing = self._active_by_session.get(session_id)
            if existing:
                raise ActiveRunError(existing.id)

            state = RunState(
                id=uuid4().hex,
                session_id=session_id,
                cwd=cwd,
                kind=kind,
                user_message=copy.deepcopy(user_message),
                session_base=session_base,
                session_totals=session_base,
                base_messages=copy.deepcopy(base_messages),
                agent=agent,
                on_complete=on_complete,
                wake=wake,
                lock=lock,
                on_queued_turn=on_queued_turn,
                tap=tap,
                on_child_complete=on_child_complete,
            )
            state.task = asyncio.create_task(self._run(state), name=f"mycode-run-{state.id}")
            self._active_by_session[session_id] = state
            self._runs_by_id[state.id] = state
            self._publish("run_started", state)
            return state.info()

    async def get_run(self, run_id: str) -> RunState | None:
        await self._prune_finished_runs()
        async with self._lock:
            return self._runs_by_id.get(run_id)

    async def get_active_run(self, session_id: str) -> RunState | None:
        async with self._lock:
            return self._active_by_session.get(session_id)

    async def enqueue(self, state: RunState, message: ConversationMessage) -> bool:
        """Queue a message for the next turn of an active chat run."""

        async with self._lock:
            if (
                state.kind != "chat"
                or state.cancel_requested
                or self._active_by_session.get(state.session_id) is not state
            ):
                return False
            state.queue.append(copy.deepcopy(message))
            return True

    async def remove_queued(self, session_id: str, input_id: str) -> dict[str, Any] | None:
        """Remove one pending queued message; returns the run, or ``None`` when it is not pending."""

        async with self._lock:
            state = self._active_by_session.get(session_id)
            if state is None:
                return None
            for index, message in enumerate(state.queue):
                if message.get("meta", {}).get("input_id") == input_id:
                    del state.queue[index]
                    return state.info()
            return None

    async def steer_queued(self, state: RunState, input_id: str) -> bool | None:
        """Move one queued message to the run's steers.

        Returns ``None`` when it is not pending, ``False`` when the run is not
        accepting steers (the message stays queued).
        """

        async with self._lock:
            for index, message in enumerate(state.queue):
                if message.get("meta", {}).get("input_id") == input_id:
                    if not state.agent.steer(message):
                        return False
                    del state.queue[index]
                    return True
            return None

    async def snapshot_session(self, session_id: str) -> dict[str, Any] | None:
        """Return a reconnect snapshot for the active session."""

        async with self._lock:
            state = self._active_by_session.get(session_id)

        if not state:
            return None

        async with state.condition:
            if state.status != "running":
                return None
            history = state.committed[: state.history_len]
            if not history and state.user_message is not None:
                # Not committed yet: the input still leads the turn.
                history = [state.user_message]
            return {
                "run": state.info(),
                "messages": copy.deepcopy(state.base_messages + history),
                "pending_events": [event for event in state.events if event["seq"] > state.history_seq],
                # Job notifications are not user input; the client never restores them.
                "pending": {
                    "steers": copy.deepcopy([m for m in state.agent.pending_steers() if not is_job_message(m)]),
                    "queue": copy.deepcopy([m for m in state.delivering + state.queue if not is_job_message(m)]),
                },
                "totals": state.session_totals,
            }

    @asynccontextmanager
    async def session_operation(self, session_id: str) -> AsyncGenerator[None]:
        # Keep a strong reference while waiting and holding the session lock.
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
                pending = []
                for event in reversed(state.events):
                    if event["seq"] <= last_seq:
                        break
                    pending.append(event)
                pending.reverse()
                finished = state.status != "running"

                if not pending and not finished:
                    await state.condition.wait()
                    continue

            for payload in pending:
                yield payload
                last_seq = payload["seq"]

            if finished:
                break

    async def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        state = await self.get_run(run_id)
        if not state:
            return None
        self.request_cancel(state)
        if state.task is not None:
            # Request cancellation must not interrupt the run's own cleanup.
            await asyncio.shield(state.task)
        return state.info()

    def request_cancel(self, state: RunState) -> None:
        """Stop a running run like a user Stop, once; ``cancel_run`` also waits for it to end."""

        # Repeated task.cancel() can interrupt a tool's cancellation cleanup.
        if state.cancel_requested or state.status != "running":
            return
        state.cancel_requested = True
        state.agent.cancel()
        for fut in state.pending_decisions.values():
            if not fut.done():
                fut.cancel()

    async def aclose(self) -> None:
        """Cancel and await owned runs after the application stops handling requests."""

        # No wake may start while the runs wind down.
        for jobs in self._jobs.values():
            jobs.suspended = True
        async with self._lock:
            tasks = []
            for state in self._runs_by_id.values():
                if state.task is not None and not state.task.done():
                    self.request_cancel(state)
                    tasks.append(state.task)
        await asyncio.gather(*tasks)
        await asyncio.gather(*(jobs.close() for jobs in self._jobs.values()))
        self._jobs.clear()
        self._runs_by_id.clear()
        self._active_by_session.clear()
        self._session_locks.clear()

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
        agent: str | None = None,
    ) -> ToolReviewDecision:
        """Emit permission_request, await the user's decision, then emit permission_resolved.

        ``agent`` names the blocking subagent whose call this is; its request
        waits on the parent session's run.
        """

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
            {
                "type": "permission_request",
                "request_id": request_id,
                "tool_use_id": tool_call_id,
                "tool_name": tool_name,
                "preview": preview,
                "agent": agent,
            },
        )

        decision: ToolReviewDecision = "deny"
        try:
            decision = await future
            if decision == "deny":
                self.request_cancel(state)
            return decision
        except asyncio.CancelledError:
            # Treat cancellation as deny so the agent loop unwinds via its own cancel check.
            return decision
        finally:
            state.pending_decisions.pop(request_id, None)
            await self._append_event(
                state, {"type": "permission_resolved", "request_id": request_id, "decision": decision}
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
        """Run the agent, store streamed events, and finish the run."""

        started = time.monotonic()
        last_error: str | None = None
        # A run can be stopped before the SDK operation has started.
        cancelled = state.cancel_requested
        if not cancelled:
            last_error, cancelled = await self._drive(state)

        status: RunStatus = "completed"
        terminal: ErrorEvent | CancelledEvent | None = None
        if last_error is not None:
            status, terminal = "failed", ErrorEvent(message=last_error)
        elif cancelled or state.cancel_requested:
            status, terminal = "cancelled", CancelledEvent()
            await self._append_event(state, terminal)

        if state.on_child_complete is not None:
            outcome = ChildOutcome.from_records(
                state.agent.messages[len(state.base_messages) :],
                terminal=terminal,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            try:
                await state.on_child_complete(outcome)
            except Exception:
                # The outcome is set; the run still ends with the turn's status.
                logger.exception("subagent turn completion failed")
        await self._finish_run(state, status=status, error=last_error)

    async def _drive(self, state: RunState) -> tuple[str | None, bool]:
        """Run the agent operation; return the run's error and whether it was cancelled."""

        last_error: str | None = None
        cancelled = False

        try:
            if state.kind == "compact":
                # The agent persists the compact marker before this returns.
                marker = await state.agent.acompact()
                if state.on_complete is not None:
                    await state.on_complete(state.session_id)
                await self._append_event(
                    state, CompactEvent(trigger="manual"), compact_totals=sum_session_totals([marker])
                )
            else:
                assert state.user_message is not None
                message = state.user_message
                on_persist = partial(self._record_commit, state)
                while True:
                    async with aclosing(state.agent.achat(message, on_persist=on_persist)) as stream:
                        async for event in stream:
                            # achat() commits its user message before its first
                            # event, so a queued turn is announced only once it
                            # is in the session; a failed commit reaches the
                            # client as an error with nothing delivered.
                            if state.delivering:
                                await self._append_event(state, UserMessageEvent(message=message))
                                state.delivering = []
                            if event.type == "retry":
                                continue
                            if event.type == "error":
                                last_error = event.message or "unknown error"
                            elif event.type == "cancelled":
                                cancelled = True
                                continue
                            await self._append_event(state, event)
                    if last_error is not None or cancelled:
                        break
                    if state.kind == "agent":
                        # One turn, no queue; the run stays active until its outcome is saved.
                        break
                    # Stop requests and enqueue() run on this loop, so they land
                    # either before this hold or once the next achat() has its
                    # run: a stop after the take still ends the queued turn.
                    async with self._lock:
                        queued, state.queue = state.queue, []
                        # A stop drops the queue; the client restores its items.
                        if state.cancel_requested:
                            break
                        if not queued:
                            # Leaving the active map with the final take means
                            # no later enqueue is accepted and then dropped;
                            # settling the job registry in the same step means
                            # this run never releases results a wake has taken.
                            self._release_session(state, status="completed")
                            break
                        # The queued turn continues on the same agent and model.
                        state.delivering = queued
                        message = merge_user_messages(queued, steer=False)
                    # Recorded while the run still holds the session's claim;
                    # an item removed from the queue first never touches the catalog.
                    if state.on_queued_turn is not None:
                        await state.on_queued_turn(message)
        except asyncio.CancelledError:
            # Manual compaction and external task cancellation both end the run.
            cancelled = True
        except Exception as exc:
            # Reached by compact failures (e.g. nothing to compact, provider
            # errors) and by persist/cleanup failures during a chat turn.
            last_error = str(exc)
            await self._append_event(state, ErrorEvent(message=last_error))
        return last_error, cancelled

    async def _record_commit(self, state: RunState, message: ConversationMessage) -> None:
        async with state.condition:
            state.committed.append(message)
            # A steer or queued message enters history with its user_message
            # event, which clients apply as the start of its segment.
            if message["role"] == "user" and "input_ids" not in message.get("meta", {}):
                state.history_len = len(state.committed)
                state.history_seq = state.next_seq - 1

    async def _append_event(
        self, state: RunState, event: Event | dict[str, Any], *, compact_totals: SessionTotals | None = None
    ) -> None:
        """Buffer one stream event, an SDK event or a server-only payload, projected to its SSE shape."""

        async with state.condition:
            payload = event if isinstance(event, dict) else event.to_dict()
            match event:
                case UserMessageEvent():
                    # A delivered steer or queued turn starts a new usage segment.
                    state.session_base = state.session_totals
                    state.history_len = len(state.committed)
                    state.history_seq = state.next_seq
                case UsageEvent():
                    state.session_totals = state.session_base.add(event.turn_usage, event.turn_cost)
                    payload |= {
                        "context_window": state.agent.context_window,
                        "model": state.agent.model,
                        **state.session_totals.payload(),
                    }
                case CompactEvent() if compact_totals is not None:
                    state.session_totals = state.session_base.add(compact_totals.usage, compact_totals.cost)
                case ToolDoneEvent():
                    # A subagent's spend is in its own session, not in this
                    # run's turn totals: a blocking agent call's result
                    # metadata, the only tool metadata with ``usage`` and
                    # ``cost``, carries what the child turn cost, which joins
                    # the session base and the current totals, so snapshots
                    # and later usage events include it. Disk totals count the
                    # child session itself, so nothing is counted twice when
                    # the session is loaded.
                    metadata = event.metadata or {}
                    usage, cost = metadata.get("usage"), metadata.get("cost")
                    state.session_base = state.session_base.add(usage, cost)
                    state.session_totals = state.session_totals.add(usage, cost)
                    retained = [
                        buffered
                        for buffered in state.events
                        if buffered.get("type") != "tool_output" or buffered.get("tool_use_id") != event.tool_use_id
                    ]
                    state.events = retained
                    state.tool_output_bytes = sum(_tool_output_bytes(buffered) for buffered in retained)

            # The buffer holds web API events: SSE and reconnect snapshots share this shape.
            payload = StreamEvent(seq=state.next_seq, **payload).model_dump(exclude_none=True)
            state.next_seq += 1
            state.events.append(payload)
            state.tool_output_bytes += _tool_output_bytes(payload)
            while len(state.events) > RUN_EVENT_BUFFER_SIZE or state.tool_output_bytes > RUN_TOOL_OUTPUT_BUFFER_BYTES:
                removed = state.events.pop(0)
                state.tool_output_bytes -= _tool_output_bytes(removed)
            state.condition.notify_all()
            if state.tap is not None and not isinstance(event, dict):
                state.tap(event)

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
            self._release_session(state, status=status)
            self._publish("run_finished", state)

        jobs = self._jobs.get(state.session_id)
        if jobs is not None:
            # The run is over, so the registry decides again what to do with its pending results.
            await jobs.deliver_pending()

    def _release_session(self, state: RunState, *, status: RunStatus) -> None:
        """Free the session's active slot and settle its job registry in the same step.

        Called under ``self._lock``. Once the slot is free, a wake can start
        and take the pending results, so this run's results are settled first:
        ``agent.messages`` holds only committed records, so the blocks found
        there are delivered and the rest return to pending. A user stop, or a
        wake whose own message never committed, holds the next wake until the
        user's next message. The session's claim goes with the slot, so the
        next run can take it.
        """

        if self._active_by_session.get(state.session_id) is not state:
            return
        del self._active_by_session[state.session_id]
        if state.lock is not None:
            state.lock.release()
        jobs = self._jobs.get(state.session_id)
        if jobs is None:
            return
        jobs.reconcile(state.agent.messages)
        committed_any = len(state.agent.messages) > len(state.base_messages)
        if status == "cancelled" or (state.wake and not committed_any):
            jobs.suspended = True

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


@dataclass(eq=False)
class AgentRunTurn(ChildTurn):
    """A child turn run as an agent run; ``cancel()`` stops that run like a user Stop."""

    runs: RunManager = field(kw_only=True)
    # Set once the run has started; a cancel before then is latched and applied at the start.
    state: RunState | None = field(default=None, init=False)

    @override
    def cancel(self) -> None:
        self.cancel_requested = True
        if self.state is not None:
            self.runs.request_cancel(self.state)


@dataclass(kw_only=True)
class ServerAgentHost(LocalAgentHost):
    """The ``AgentHost`` of the web server: each child turn is an ``agent`` run of
    the run manager, keyed ``<parent id>/<child id>``, streaming on its own run id.

    Built with the parent agent for each chat, wake and compact run, with the parent's review.
    """

    runs: RunManager

    @override
    def start_turn(
        self, agent: Agent, message: str, *, session_id: str, description: str, on_complete: OnChildComplete
    ) -> AgentRunTurn:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        async def events() -> AsyncIterator[Event]:
            while (event := await queue.get()) is not None:
                yield event

        turn = AgentRunTurn(agent=agent, events=events(), runs=self.runs)

        def tap(event: Event) -> None:
            turn.run_started()
            queue.put_nowait(event)

        async def complete(outcome: ChildOutcome) -> None:
            turn.outcome = outcome
            await on_complete(outcome)

        async def drive() -> None:
            try:
                run = await self.runs.start_run(
                    kind="agent",
                    session_id=f"{self.session_id}/{session_id}",
                    cwd=self.settings.cwd,
                    user_message=user_text_message(message),
                    base_messages=agent.messages,
                    agent=agent,
                    session_base=await self.children.load_totals(session_id),
                    tap=tap,
                    on_child_complete=complete,
                )
            except Exception as exc:
                # A turn that cannot start fails like one whose provider failed.
                failed = ErrorEvent(message=str(exc) or type(exc).__name__)
                await complete(ChildOutcome.from_records([], terminal=failed, duration_ms=0))
                return
            state = await self.runs.get_run(run["id"])
            assert state is not None
            assert state.task is not None
            # A cancel latched before the run existed: _run sees it before
            # achat() starts, or the stop reaches the started operation.
            turn.state = state
            if turn.cancel_requested:
                self.runs.request_cancel(state)
            await state.task

        turn.task = asyncio.create_task(drive())
        # The events end only once the turn, outcome included, is done.
        turn.task.add_done_callback(lambda _: queue.put_nowait(None))
        return turn
