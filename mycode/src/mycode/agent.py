"""Multi-turn agent loop.

:class:`Agent` drives one conversation. Each call to :meth:`Agent.achat`
runs one user turn; when a ``session_dir`` is configured, every emitted
message is appended to the on-disk session log.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mycode.attachments import AttachmentLike, build_attachment_blocks
from mycode.compact import (
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_COMPACT_THRESHOLD,
    CompactTrigger,
    NothingToCompactError,
    build_compact_event,
    has_compactable_history,
    should_compact,
)
from mycode.hooks import Hooks, ToolHookContext
from mycode.messages import (
    USAGE_TOKEN_KEYS,
    ConversationMessage,
    build_message,
    flatten_message_text,
    tool_result_block,
    user_text_message,
)
from mycode.models import Cost, estimate_cost, infer_provider_from_model, resolve_model_metadata
from mycode.providers import get_provider_adapter
from mycode.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    StreamStartTimeoutError,
)
from mycode.session import SessionStore
from mycode.tools import (
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolSpec,
)

logger = logging.getLogger(__name__)

PersistCallback = Callable[[ConversationMessage], Awaitable[None]]
_LIVE_OUTPUT_OMISSION = "[live output omitted]"
# Bound on pending live output per tool call; the overflow is replaced by
# _LIVE_OUTPUT_OMISSION rather than blocking the tool.
_LIVE_OUTPUT_MAX_BYTES = 50 * 1024


class _RunCancelled(BaseException):
    """A user stop request, distinct from cancellation of the caller's task."""


async def _finish_task[T](task: asyncio.Future[T]) -> T:
    """Finish owned cleanup or persistence before propagating caller cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


@dataclass
class _RunState:
    loop: asyncio.AbstractEventLoop
    cancel_requested: bool = False
    active_task: asyncio.Future[Any] | None = None

    def cancel_active_task(self) -> None:
        task = self.active_task
        self.active_task = None
        if task is None or task.done():
            return
        # Clearing the reference protects cleanup even if the tool calls uncancel().
        if not isinstance(task, asyncio.Task) or not task.cancelling():
            task.cancel()

    def cancel(self) -> None:
        if not self.cancel_requested:
            self.cancel_requested = True
            # A hook requesting the stop itself must be able to finish its refusal.
            if self.active_task is not asyncio.current_task():
                self.cancel_active_task()

    def check_cancelled(self) -> None:
        if self.cancel_requested:
            raise _RunCancelled

    @asynccontextmanager
    async def task[T](self, awaitable: Awaitable[T]) -> AsyncGenerator[asyncio.Future[T], None]:
        """Own one interruptible task, including its cancellation cleanup."""

        caller = asyncio.current_task()
        assert caller is not None
        cancelling = caller.cancelling()
        task = asyncio.ensure_future(awaitable)
        self.active_task = task
        if self.cancel_requested:
            self.cancel_active_task()
        try:
            yield task
        except asyncio.CancelledError:
            # Either the task running this scope was cancelled from outside
            # (for example by asyncio.timeout), or run.cancel() stopped the
            # owned task. An outside cancellation propagates, and the stop is
            # recorded first so nested scopes and cleanup still wind down; a
            # stop of the owned task is reported as _RunCancelled instead.
            # The cancel count separates the two cases: a count above the one
            # recorded on entry came from outside. A task that did not enter
            # the scope has no recorded count, so any pending cancellation in
            # it counts as outside.
            current = asyncio.current_task()
            assert current is not None
            baseline = cancelling if current is caller else 0
            if current.cancelling() > baseline or not self.cancel_requested:
                self.cancel_requested = True
                raise
            raise _RunCancelled from None
        except GeneratorExit:
            self.cancel_requested = True
            raise
        finally:
            try:
                if not task.done():
                    self.cancel_active_task()
                    (outcome,) = await _finish_task(asyncio.gather(task, return_exceptions=True))
                else:
                    outcome = None if task.cancelled() else task.exception()
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, (_RunCancelled, asyncio.CancelledError)
                ):
                    raise outcome
            finally:
                self.active_task = None


class _ToolOutputBuffer:
    """Thread-safe bounded buffer for live tool output."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._omitted = False
        self._finished = False
        self._notified = False
        self._at_line_boundary = True

    def append(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            if self._finished:
                return
            self._pending.extend(delta.encode("utf-8"))
            excess = len(self._pending) - _LIVE_OUTPUT_MAX_BYTES
            if excess > 0:
                del self._pending[:excess]
                self._omitted = True
            if self._notified:
                return
            self._notified = True
        self._loop.call_soon_threadsafe(self._event.set)

    def finish(self) -> None:
        with self._lock:
            self._finished = True
            if self._notified:
                return
            self._notified = True
        self._loop.call_soon_threadsafe(self._event.set)

    async def get(self) -> str | None:
        await self._event.wait()
        with self._lock:
            pending = self._pending.decode("utf-8", errors="ignore")
            omitted = self._omitted
            finished = self._finished
            self._pending.clear()
            self._omitted = False
            self._notified = False
            self._event.clear()
            resignal = bool(pending and finished)
            if resignal:
                self._notified = True

            if pending and omitted:
                separator = "" if self._at_line_boundary else "\n"
                pending = f"{separator}{_LIVE_OUTPUT_OMISSION}\n{pending}"
            if pending:
                self._at_line_boundary = pending.endswith("\n")

        if resignal:
            self._event.set()
        if pending:
            return pending
        return None if finished else ""


@dataclass
class Event:
    """Streaming event emitted by :meth:`Agent.achat`."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    """Collected result returned by :meth:`Agent.run`."""

    text: str = ""
    events: list[Event] = field(default_factory=list)
    error: str | None = None
    usage: dict[str, Any] | None = None
    cancelled: bool = False


def _accumulate_usage(
    turn_usage: dict[str, Any],
    turn_cost: Cost | None,
    usage: dict[str, Any],
    request_cost: Cost | None,
) -> Cost | None:
    """Fold one provider request's usage into the turn accumulator.

    Mutates ``turn_usage`` and returns the updated best-effort turn cost.
    """

    for key in USAGE_TOKEN_KEYS:
        value = usage.get(key)
        if value is None:
            continue
        turn_usage[key] = turn_usage.get(key, 0) + value
    if request_cost is None:
        return turn_cost
    if turn_cost is None:
        return request_cost

    total = turn_cost["total"] + request_cost["total"]
    has_details = all("input" in cost and "output" in cost for cost in (turn_cost, request_cost))
    if not has_details:
        return {"total": total}

    result: Cost = {
        "total": total,
        "input": turn_cost.get("input", 0.0) + request_cost.get("input", 0.0),
        "output": turn_cost.get("output", 0.0) + request_cost.get("output", 0.0),
    }
    cache_read = turn_cost.get("cache_read", 0.0) + request_cost.get("cache_read", 0.0)
    cache_write = turn_cost.get("cache_write", 0.0) + request_cost.get("cache_write", 0.0)
    reasoning = turn_cost.get("reasoning", 0.0) + request_cost.get("reasoning", 0.0)
    if "cache_read" in turn_cost or "cache_read" in request_cost:
        result["cache_read"] = cache_read
    if "cache_write" in turn_cost or "cache_write" in request_cost:
        result["cache_write"] = cache_write
    if "reasoning" in turn_cost or "reasoning" in request_cost:
        result["reasoning"] = reasoning
    return result


def _created_at(message: ConversationMessage) -> datetime | None:
    raw = (message.get("meta") or {}).get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _turn_elapsed_ms(turn_start: ConversationMessage, record: ConversationMessage) -> int | None:
    """Elapsed time between two committed records, read from their stamps.

    Subtracting the stamps rather than keeping a separate clock is what makes
    the streamed value equal the one a reloaded session derives from JSONL.
    """

    started = _created_at(turn_start)
    ended = _created_at(record)
    if started is None or ended is None:
        return None
    return max(0, int((ended - started).total_seconds() * 1000))


class Agent:
    """Multi-turn tool-calling agent runtime."""

    def __init__(
        self,
        *,
        model: str,
        provider: str | None = None,
        session_dir: Path | None = None,
        session_id: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        messages: list[ConversationMessage] | None = None,
        max_turns: int | None = None,
        max_tokens: int | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        stream_start_timeout: float = 60.0,
        max_retries: int = 2,
        context_window: int | None = None,
        compact_threshold: float | None = None,
        reasoning_effort: str | None = None,
        supports_reasoning_effort: bool = False,
        legacy_max_tokens: bool = False,
        supports_image_input: bool | None = None,
        supports_pdf_input: bool | None = None,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        hooks: Hooks | None = None,
        deps: object | None = None,
    ):
        self.model = model
        if provider is None:
            inferred = infer_provider_from_model(model)
            if inferred is None:
                raise ValueError(f"could not infer provider for model {model!r}; pass provider= explicitly")
            provider = inferred
        self.provider = provider

        # Opaque application context handed to every tool and hook; the SDK
        # never reads it.
        self.deps = deps

        # Persistence is opt-in: a store is only created when ``session_dir``
        # is supplied. ``session_id`` is always populated (uuid when absent)
        # so Events can carry a stable runtime tag even in memory-only mode.
        self.session_dir = session_dir
        self.session_id = (session_id or "").strip() or uuid4().hex
        self._store: SessionStore | None = SessionStore(data_dir=session_dir) if session_dir is not None else None
        self.transcript_path: str | None = str(self._store.messages_path(self.session_id)) if self._store else None

        self.api_key = api_key
        self.api_base = api_base
        self.max_turns = max_turns
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if stream_start_timeout <= 0:
            raise ValueError("stream_start_timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.request_timeout = float(request_timeout)
        self.stream_start_timeout = float(stream_start_timeout)
        self.max_retries = int(max_retries)
        self.compact_threshold = compact_threshold if compact_threshold is not None else DEFAULT_COMPACT_THRESHOLD
        self.reasoning_effort = reasoning_effort
        # Whether the endpoint accepts the effort knob. Comes from provider
        # config, not the catalog, so it stays out of refresh_capabilities.
        self.supports_reasoning_effort = supports_reasoning_effort
        # Whether the endpoint only implements the legacy max_tokens field.
        self.legacy_max_tokens = legacy_max_tokens

        self.system = system
        self.hooks = hooks or Hooks()
        self._active_run: _RunState | None = None

        # History resolution:
        # - messages is None → auto-resume from disk if the session exists
        # - messages is [] or [...] → use as-is; refuse if it would overwrite disk
        if messages is None:
            messages = self._store.load_messages_sync(self.session_id) if self._store is not None else []
        elif self._store is not None and self._store.session_exists(self.session_id):
            msg = (
                f"session {self.session_id!r} already exists on disk; "
                "pass messages=None to resume or choose a different session_id"
            )
            raise ValueError(msg)
        self.messages: list[ConversationMessage] = list(messages)

        self.tools = ToolExecutor(tools)

        self.refresh_capabilities(
            max_tokens=max_tokens,
            context_window=context_window,
            supports_image_input=supports_image_input,
            supports_pdf_input=supports_pdf_input,
        )

    def refresh_capabilities(
        self,
        *,
        max_tokens: int | None = None,
        context_window: int | None = None,
        supports_image_input: bool | None = None,
        supports_pdf_input: bool | None = None,
    ) -> None:
        """Resolve model capability fields against the metadata catalog.

        Explicit arguments win; otherwise the bundled catalog supplies the value;
        otherwise a conservative default is used. Call this after mutating
        ``self.provider`` or ``self.model`` to re-derive the capability fields.
        """

        meta = resolve_model_metadata(
            provider=self.provider,
            model=self.model,
            max_output_tokens=max_tokens,
            context_window=context_window,
            supports_image_input=supports_image_input,
            supports_pdf_input=supports_pdf_input,
        )
        self.max_tokens: int = meta.max_output_tokens or 16_384
        self.context_window: int = meta.context_window or 128_000
        self.model_pricing: dict[str, Any] | None = meta.pricing
        self.supports_image_input: bool = bool(meta.supports_image_input)
        self.supports_pdf_input: bool = bool(meta.supports_pdf_input)

    def cancel(self) -> None:
        """Request a user stop; the active operation finishes its own cleanup."""

        run = self._active_run
        if run is None:
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is run.loop:
            run.cancel()
            return
        try:
            run.loop.call_soon_threadsafe(run.cancel)
        except RuntimeError:
            # The captured run may have finished and closed its loop meanwhile.
            if self._active_run is run:
                raise

    def clear(self) -> None:
        """Drop the in-memory conversation history."""

        if self._active_run is not None:
            raise RuntimeError("cannot clear history during an active operation")
        self.messages = []

    async def achat(
        self,
        user_input: str | ConversationMessage,
        *,
        attachments: Sequence[AttachmentLike] = (),
        on_persist: PersistCallback | None = None,
    ) -> AsyncGenerator[Event, None]:
        """Run the full agent loop for one user message."""

        terminal: Event | None = None
        async with self._run_scope() as run:
            try:
                async with aclosing(self._achat(run, user_input, attachments, on_persist)) as stream:
                    async for event in stream:
                        if event.type == "error":
                            terminal = event
                            break
                        yield event
                if terminal is None:
                    run.check_cancelled()
            except _RunCancelled:
                terminal = Event("cancelled")
        if terminal is not None:
            yield terminal

    def run(
        self,
        user_input: str | ConversationMessage,
        *,
        attachments: Sequence[AttachmentLike] = (),
        on_persist: PersistCallback | None = None,
    ) -> RunResult:
        """Run one user turn synchronously and collect the streamed result."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Agent.run() cannot run inside an active event loop; use Agent.achat() instead")

        async def collect() -> RunResult:
            result = RunResult()
            text_parts: list[str] = []
            async for event in self.achat(user_input, attachments=attachments, on_persist=on_persist):
                if event.type != "tool_output":
                    result.events.append(event)
                if event.type == "text":
                    text_parts.append(str(event.data.get("delta") or ""))
                elif event.type == "usage":
                    result.usage = event.data
                elif event.type == "error" and result.error is None:
                    result.error = str(event.data.get("message") or "")
                elif event.type == "cancelled":
                    result.cancelled = True
            result.text = "".join(text_parts)
            return result

        return asyncio.run(collect())

    async def acompact(
        self,
        *,
        on_persist: PersistCallback | None = None,
    ) -> ConversationMessage:
        """Compact now; raise NothingToCompactError for empty context or CancelledError on stop."""

        try:
            async with self._run_scope() as run:
                adapter = get_provider_adapter(self.provider)
                marker = await self._summarize(run, adapter, trigger="manual")
                await self._commit(marker, on_persist)
                run.check_cancelled()
                return marker
        except _RunCancelled:
            raise asyncio.CancelledError from None

    def compact(
        self,
        *,
        on_persist: PersistCallback | None = None,
    ) -> ConversationMessage:
        """Compact the conversation synchronously; see :meth:`acompact`."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Agent.compact() cannot run inside an active event loop; use Agent.acompact() instead")

        return asyncio.run(self.acompact(on_persist=on_persist))

    @asynccontextmanager
    async def _run_scope(self) -> AsyncGenerator[_RunState, None]:
        if self._active_run is not None:
            raise RuntimeError("Agent already has an active operation")
        run = _RunState(asyncio.get_running_loop())
        self._active_run = run
        try:
            yield run
        finally:
            self._active_run = None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _run_tool_call(
        self, run: _RunState, tool_use: dict[str, Any], stop_reason: str
    ) -> AsyncGenerator[Event, None]:
        """Run one tool call and emit the standard tool events."""

        tool_id = str(tool_use.get("id") or "")
        name = str(tool_use.get("name") or "")
        raw_args = tool_use.get("input")
        args = raw_args if isinstance(raw_args, dict) else {}

        # Surface the tool to the UI before running hooks so the call is
        # always visible — even when a hook is awaiting a permission decision.
        yield Event("tool_start", {"tool_call": {"id": tool_id, "name": name, "input": args}})

        block_meta = tool_use.get("meta") or {}
        if stop_reason == "length":
            yield self._error_done(
                tool_id, "error: tool call was truncated by the output token limit and was not executed"
            )
            return
        if block_meta.get("invalid_input") is True:
            yield self._error_done(
                tool_id,
                "\n".join(
                    [
                        f"error: invalid arguments for {tool_use.get('name')} and the call was not executed.",
                        f"parse error: {block_meta.get('parse_error')}",
                        f"raw arguments: {block_meta.get('raw_arguments')}",
                    ]
                ),
            )
            return
        if stop_reason not in {"stop", "tool_use"}:
            yield self._error_done(
                tool_id, "error: provider response did not complete the tool call and it was not executed"
            )
            return
        if run.cancel_requested:
            yield self._error_done(tool_id, "error: cancelled")
            return

        spec = self.tools.get(name)
        if spec is None:
            yield self._error_done(tool_id, f"error: unknown tool: {name}")
            return

        hook_ctx = ToolHookContext(
            session_id=self.session_id,
            deps=self.deps,
            provider=self.provider,
            model=self.model,
            tool_call_id=tool_id,
            tool_name=name,
            tool_input=args,
            tool=spec,
        )
        output_buffer = _ToolOutputBuffer(run.loop) if spec.streams_output else None
        ctx = ToolContext(
            executor=self.tools,
            deps=self.deps,
            supports_image_input=self.supports_image_input,
            tool_call_id=tool_id,
            emit=output_buffer.append if output_buffer else None,
        )
        try:
            async with run.task(self._execute_tool(run, hook_ctx, ctx, args)) as task:
                if output_buffer is not None:
                    # The done callback wakes the consumer even when the tool
                    # task is cancelled before its coroutine starts.
                    task.add_done_callback(lambda _task: output_buffer.finish())
                    while (output := await output_buffer.get()) is not None:
                        if output and not run.cancel_requested:
                            yield Event("tool_output", {"tool_use_id": tool_id, "output": output})
                result = await asyncio.shield(task)
        except _RunCancelled:
            result = ToolExecutionResult(output="error: cancelled", is_error=True)
        yield self._tool_done_event(tool_id, result)

    async def _execute_tool(
        self,
        run: _RunState,
        hook_ctx: ToolHookContext[Any],
        ctx: ToolContext[Any],
        args: dict[str, Any],
    ) -> ToolExecutionResult:
        """One execution policy for hooks and both streaming and non-streaming tools."""

        try:
            result = await self.hooks.run_before_tool(hook_ctx, check_cancelled=run.check_cancelled)
        except Exception as exc:
            return ToolExecutionResult(output=f"error: tool hook failed: {exc}", is_error=True)
        # run.cancel() leaves active_task set when the stop is requested from
        # inside this task, so the intact slot means a hook stopped its own
        # call and its refusal result survives. An outside stop cancels this
        # task and discards whatever the hooks returned.
        if (
            run.cancel_requested
            and result is not None
            and result.is_error
            and run.active_task is asyncio.current_task()
        ):
            return result
        run.check_cancelled()

        if result is None:
            try:
                result = await self.tools.aexecute(hook_ctx.tool_name, args, ctx)
            except Exception as exc:
                result = ToolExecutionResult(output=f"error: {exc}", is_error=True)
        if run.cancel_requested:
            return replace(result, is_error=True)

        try:
            result = await self.hooks.run_after_tool(hook_ctx, result, check_cancelled=run.check_cancelled)
        except Exception:
            logger.exception(
                "after_tool hook failed for %s (call %s)",
                hook_ctx.tool_name,
                hook_ctx.tool_call_id,
            )
        run.check_cancelled()
        return result

    @staticmethod
    def _tool_done_event(tool_id: str, result: ToolExecutionResult) -> Event:
        data: dict[str, Any] = {
            "tool_use_id": tool_id,
            "output": result.output,
            "is_error": result.is_error,
        }
        if result.metadata:
            data["metadata"] = result.metadata
        if result.content:
            data["content"] = result.content
        return Event("tool_done", data)

    def _error_done(self, tool_id: str, message: str) -> Event:
        """Build a tool_done event carrying an error result."""

        return self._tool_done_event(tool_id, ToolExecutionResult(output=message, is_error=True))

    # ------------------------------------------------------------------
    # Provider streaming
    # ------------------------------------------------------------------

    async def _stream_provider_turn(
        self,
        run: _RunState,
        adapter: ProviderAdapter,
        request: ProviderRequest,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        """Stream one provider turn, retrying failed attempts before visible output.

        Yields an internal ``retry`` event before each new attempt. Once a
        thinking or text delta has been yielded, failures propagate instead
        of retrying. Adapter ``stream_started`` markers are consumed here
        and never reach the caller.
        """

        max_attempts = self.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            visible_output_emitted = False
            try:
                async with aclosing(self._stream_provider_attempt(run, adapter, request)) as stream:
                    async for event in stream:
                        if event.type == "stream_started":
                            continue
                        if event.type in {"thinking_delta", "text_delta"}:
                            visible_output_emitted = True
                        yield event
                return
            except ProviderError as exc:
                if run.cancel_requested or visible_output_emitted or not exc.retryable or attempt >= max_attempts:
                    raise
                delay = self._retry_delay(attempt, exc)
                retry_data: dict[str, Any] = {
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "delay_seconds": round(delay, 3),
                    "reason": exc.reason,
                    "message": str(exc),
                }
                if exc.status_code is not None:
                    retry_data["status_code"] = exc.status_code
                yield ProviderStreamEvent("retry", retry_data)
                async with run.task(asyncio.sleep(delay)) as wait:
                    await asyncio.shield(wait)
                run.check_cancelled()

    async def _stream_provider_attempt(
        self,
        run: _RunState,
        adapter: ProviderAdapter,
        request: ProviderRequest,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        """Iterate one provider attempt with cancellation and the start deadline.

        The deadline covers everything up to the first upstream event: DNS,
        connect, request upload, response headers, and the wait for the first
        SSE event or chunk.
        """

        provider_stream: AsyncIterator[ProviderStreamEvent] = adapter.stream_turn(request)
        started = False

        try:
            while True:
                run.check_cancelled()
                try:
                    async with run.task(anext(provider_stream)) as task:
                        if started:
                            event = await asyncio.shield(task)
                        else:
                            try:
                                event = await asyncio.wait_for(asyncio.shield(task), self.stream_start_timeout)
                            except TimeoutError:
                                raise StreamStartTimeoutError(
                                    f"no provider stream event received within {self.stream_start_timeout:g}s"
                                ) from None
                except StopAsyncIteration:
                    return

                run.check_cancelled()
                started = True
                yield event
        finally:
            # Runs before the retry loop backs off, so a failed attempt's
            # stream is closed before the next attempt starts.
            close = cast(Callable[[], Awaitable[None]] | None, getattr(provider_stream, "aclose", None))
            if close is not None:
                await _finish_task(asyncio.ensure_future(close()))

    def _retry_delay(self, failed_attempts: int, error: ProviderError) -> float:
        """Backoff before the next attempt: Retry-After when sane, else exponential."""

        if error.retry_after is not None and 0 < error.retry_after <= 60:
            return error.retry_after
        base = min(8.0, 0.5 * 2 ** (failed_attempts - 1))
        return base * (1 - 0.25 * random.random())

    def _build_request(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        append_messages: Sequence[ConversationMessage] = (),
    ) -> ProviderRequest:
        """Build a ProviderRequest from the agent's current state."""

        return ProviderRequest(
            provider=self.provider,
            model=self.model,
            session_id=self.session_id,
            messages=self.messages,
            system=self.system,
            tools=self.tools.definitions if tools is None else tools,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            api_key=self.api_key,
            api_base=self.api_base,
            reasoning_effort=reasoning_effort,
            legacy_max_tokens=self.legacy_max_tokens,
            supports_image_input=self.supports_image_input,
            supports_pdf_input=self.supports_pdf_input,
            transcript_path=self.transcript_path,
            append_messages=list(append_messages),
            request_timeout=self.request_timeout,
        )

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return max(0, int((time.monotonic() - start) * 1000))

    @staticmethod
    def _stamp_thinking_duration(content: list[Any], duration_ms: int) -> None:
        """Merge duration_ms onto the last thinking block in content, if any."""

        for block in reversed(content):
            if not isinstance(block, dict) or block.get("type") != "thinking":
                continue
            raw_meta = block.get("meta")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            block["meta"] = {**meta, "duration_ms": duration_ms}
            return

    def _partial_assistant_message(
        self,
        partial_blocks: list[tuple[str, list[str]]],
        duration_ms: int | None,
        *,
        stop_reason: str | None = None,
    ) -> ConversationMessage:
        """Build the assistant message persisted for an interrupted stream."""

        partial_content = [{"type": block_type, "text": "".join(parts)} for block_type, parts in partial_blocks]
        if duration_ms is not None:
            self._stamp_thinking_duration(partial_content, duration_ms)
        meta: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "context_window": self.context_window,
        }
        if stop_reason:
            meta["stop_reason"] = stop_reason
        return build_message("assistant", partial_content, meta=meta)

    def _usage_event(
        self,
        context_tokens: int | None,
        turn_usage: dict[str, Any],
        turn_cost: Cost | None,
        turn_duration_ms: int | None,
    ) -> Event:
        """Build a usage event: the turn's cumulative totals + the context metric."""

        return Event(
            "usage",
            {
                "context_tokens": context_tokens,
                "turn_usage": dict(turn_usage),
                "turn_cost": dict(turn_cost) if turn_cost is not None else None,
                "turn_duration_ms": turn_duration_ms,
            },
        )

    def _finalize_request_message(self, message: ConversationMessage) -> tuple[dict[str, Any], Cost | None]:
        """Attach stable request metadata before the message is persisted."""

        meta = cast(dict[str, Any], message.setdefault("meta", {}))
        meta["context_window"] = self.context_window
        usage = cast(dict[str, Any], meta.get("usage") or {})
        provider_cost = meta.get("cost")
        cost = (
            cast(Cost, cast(object, provider_cost))
            if isinstance(provider_cost, dict)
            else estimate_cost(usage, self.model_pricing)
        )
        if cost is not None:
            meta["cost"] = dict(cost)
        return usage, cost

    async def _commit(
        self,
        message: ConversationMessage,
        on_persist: PersistCallback | None,
    ) -> None:
        """Commit one message to the timeline: stamp, persist, publish.

        ``meta.created_at`` is stamped here unless the caller supplied one.
        ``on_persist`` runs before the session store. The message joins
        ``self.messages`` only once its commit succeeded, so memory never runs
        ahead of the log.
        """

        meta = cast(dict[str, Any], message.setdefault("meta", {}))
        meta.setdefault("created_at", datetime.now(UTC).isoformat())

        if on_persist is None and self._store is None:
            self.messages.append(message)
            return

        async def persist() -> None:
            if on_persist is not None:
                await on_persist(message)
            if self._store is not None:
                await self._store.append_message(self.session_id, message)

        # The task lets the commit survive cancellation: once persistence has
        # started, it runs to completion before cancellation is reported.
        task = asyncio.create_task(persist())
        try:
            await _finish_task(task)
        finally:
            # exception() raises on a cancelled task, so that is checked first.
            if not task.cancelled() and task.exception() is None:
                self.messages.append(message)

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------

    async def _achat(
        self,
        run: _RunState,
        user_input: str | ConversationMessage,
        attachments: Sequence[AttachmentLike],
        on_persist: PersistCallback | None,
    ) -> AsyncGenerator[Event, None]:

        user_message: ConversationMessage
        if isinstance(user_input, str):
            user_message = user_text_message(user_input)
        else:
            if (user_input.get("role") or "user") != "user":
                yield Event("error", {"message": "user input must be a user message"})
                return
            user_message = {
                "role": "user",
                "content": [dict(b) for b in user_input.get("content") or [] if isinstance(b, dict)],
            }
            raw_meta = user_input.get("meta")
            if isinstance(raw_meta, dict):
                user_message["meta"] = {str(k): v for k, v in raw_meta.items()}

        if attachments:
            async with run.task(asyncio.to_thread(build_attachment_blocks, attachments)) as task:
                blocks = await asyncio.shield(task)
            user_message["content"].extend(blocks)

        content_blocks = user_message.get("content") or []
        for block_type, supported, label in (
            ("image", self.supports_image_input, "image input"),
            ("document", self.supports_pdf_input, "PDF input"),
        ):
            if not supported and any(
                isinstance(block, dict) and block.get("type") == block_type for block in content_blocks
            ):
                yield Event("error", {"message": f"current model does not support {label}"})
                return

        run.check_cancelled()
        await self._commit(user_message, on_persist)

        adapter = get_provider_adapter(self.provider)

        turn_usage: dict[str, Any] = {}
        turn_cost: Cost | None = None
        context_tokens: int | None = None
        turn_number = 0
        while True:
            run.check_cancelled()
            if self.max_turns is not None and turn_number >= self.max_turns:
                yield Event("error", {"message": "max_turns reached"})
                return
            turn_number += 1
            assistant_message: ConversationMessage | None = None
            partial_blocks: list[tuple[str, list[str]]] = []
            thinking_started_at: float | None = None
            thinking_duration_ms: int | None = None
            request = self._build_request(reasoning_effort=self.reasoning_effort)

            stream = self._stream_provider_turn(run, adapter, request)
            try:
                async for provider_event in stream:
                    run.check_cancelled()
                    if provider_event.type == "retry":
                        yield Event("retry", dict(provider_event.data))
                        continue

                    if provider_event.type in {"thinking_delta", "text_delta"}:
                        delta_text = str(provider_event.data.get("text") or "")
                        if not delta_text:
                            continue

                        is_thinking = provider_event.type == "thinking_delta"
                        if is_thinking:
                            if thinking_started_at is None:
                                thinking_started_at = time.monotonic()
                        elif thinking_started_at is not None and thinking_duration_ms is None:
                            thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                            yield Event("reasoning_done", {"duration_ms": thinking_duration_ms})

                        block_type = "thinking" if is_thinking else "text"
                        if partial_blocks and partial_blocks[-1][0] == block_type:
                            partial_blocks[-1][1].append(delta_text)
                        else:
                            partial_blocks.append((block_type, [delta_text]))
                        yield Event("reasoning" if is_thinking else "text", {"delta": delta_text})
                        continue

                    if provider_event.type != "message_done":
                        continue

                    if thinking_started_at is not None and thinking_duration_ms is None:
                        thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                        yield Event("reasoning_done", {"duration_ms": thinking_duration_ms})

                    message = provider_event.data.get("message")
                    if isinstance(message, dict):
                        assistant_message = message

            except (_RunCancelled, asyncio.CancelledError, GeneratorExit, Exception) as exc:
                cancelled = isinstance(exc, (_RunCancelled, asyncio.CancelledError, GeneratorExit))
                if not cancelled:
                    logger.exception("Provider request failed")
                if partial_blocks:
                    # Persist what was already streamed so the JSONL matches the
                    # visible turn; the stop_reason keeps this partial message
                    # out of provider replay.
                    if thinking_started_at is not None and thinking_duration_ms is None:
                        thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                    partial_message = self._partial_assistant_message(
                        partial_blocks, thinking_duration_ms, stop_reason="cancelled" if cancelled else "error"
                    )
                    await self._commit(partial_message, on_persist)
                if cancelled:
                    raise
                yield Event("error", {"message": str(exc)})
                return
            finally:
                # Closing errors must propagate; the generator may already be
                # unwinding and cannot yield another event.
                await stream.aclose()

            if not assistant_message:
                yield Event("error", {"message": "provider produced no assistant message"})
                return

            if thinking_duration_ms is not None:
                self._stamp_thinking_duration(assistant_message.get("content") or [], thinking_duration_ms)

            request_usage, request_cost = self._finalize_request_message(assistant_message)

            await self._commit(assistant_message, on_persist)

            context_tokens = request_usage.get("total_tokens")
            turn_cost = _accumulate_usage(turn_usage, turn_cost, request_usage, request_cost)
            elapsed_ms = _turn_elapsed_ms(user_message, assistant_message)
            yield self._usage_event(context_tokens, turn_usage, turn_cost, elapsed_ms)

            assistant_meta = assistant_message.get("meta")
            stop_reason = str(assistant_meta.get("stop_reason") or "") if isinstance(assistant_meta, dict) else ""
            if stop_reason == "error":
                yield Event("error", {"message": "provider returned an error response"})
                return

            tool_calls = [
                block
                for block in assistant_message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if tool_calls:
                tool_results: list[dict[str, Any]] = []
                for tool_call in tool_calls:
                    run.check_cancelled()
                    async with aclosing(self._run_tool_call(run, tool_call, stop_reason)) as events:
                        async for event in events:
                            yield event

                            if event.type != "tool_done":
                                continue

                            data = event.data
                            tool_results.append(
                                tool_result_block(
                                    tool_use_id=data["tool_use_id"],
                                    output=data["output"],
                                    metadata=data.get("metadata"),
                                    is_error=data["is_error"],
                                    content=data.get("content"),
                                )
                            )

                    if run.cancel_requested:
                        # Skip remaining tool calls; the results collected so
                        # far are still persisted below.
                        break

                tool_result_message = build_message("user", tool_results)
                await self._commit(tool_result_message, on_persist)

            run.check_cancelled()
            if should_compact(context_tokens, self.context_window, self.compact_threshold):
                try:
                    compact_marker = await self._summarize(run, adapter, trigger="auto")
                except Exception:
                    if run.cancel_requested:
                        raise
                    # Compaction must not block the current answer; the full
                    # transcript is still available for the next turn.
                    logger.warning(
                        "Context compaction failed, continuing without compaction",
                        exc_info=True,
                    )
                else:
                    await self._commit(compact_marker, on_persist)
                    yield Event("compact", {"trigger": "auto"})
                    # Summary usage is billed, but does not describe the normal context size.
                    compact_usage = cast(dict[str, Any], (compact_marker.get("meta") or {}).get("usage") or {})
                    compact_cost = cast(Cost | None, (compact_marker.get("meta") or {}).get("cost"))
                    turn_cost = _accumulate_usage(turn_usage, turn_cost, compact_usage, compact_cost)
                    elapsed_ms = _turn_elapsed_ms(user_message, compact_marker)
                    yield self._usage_event(context_tokens, turn_usage, turn_cost, elapsed_ms)

            if not tool_calls:
                return

    async def _summarize(
        self,
        run: _RunState,
        adapter: ProviderAdapter,
        *,
        trigger: CompactTrigger,
    ) -> ConversationMessage:
        """Build a compact marker; callers own persistence and its failures."""

        if not has_compactable_history(self.messages):
            raise NothingToCompactError("nothing to compact")

        request = self._build_request(
            tools=[],
            max_tokens=min(self.max_tokens, 8192),
            append_messages=[user_text_message(COMPACT_SUMMARY_PROMPT)],
        )

        summary_message: ConversationMessage | None = None
        async with aclosing(self._stream_provider_turn(run, adapter, request)) as stream:
            async for provider_event in stream:
                if provider_event.type == "message_done":
                    msg = provider_event.data.get("message")
                    if isinstance(msg, dict):
                        summary_message = msg

        if not summary_message:
            raise ValueError("compaction produced no response")

        summary_text = flatten_message_text(summary_message, include_thinking=False)
        if not summary_text:
            raise ValueError("compaction produced empty summary")

        run.check_cancelled()

        summary_usage, summary_cost = self._finalize_request_message(summary_message)
        return build_compact_event(
            summary_text,
            trigger=trigger,
            provider=self.provider,
            model=self.model,
            context_window=self.context_window,
            usage=summary_usage,
            cost=summary_cost,
        )
