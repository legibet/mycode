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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mycode.attachments import AttachmentLike, build_attachment_blocks
from mycode.compact import (
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_COMPACT_THRESHOLD,
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
    text_block,
    thinking_block,
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


class _ToolOutputBuffer:
    """Thread-safe bounded buffer for live tool output."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self._pending = ""
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
            self._pending, truncated = self._bounded_tail(self._pending + delta)
            self._omitted = self._omitted or truncated
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
            pending = self._pending
            omitted = self._omitted
            finished = self._finished
            self._pending = ""
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

    @staticmethod
    def _bounded_tail(text: str) -> tuple[str, bool]:
        encoded = text.encode("utf-8")
        if len(encoded) <= _LIVE_OUTPUT_MAX_BYTES:
            return text, False
        return encoded[-_LIVE_OUTPUT_MAX_BYTES:].decode("utf-8", errors="ignore"), True


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
        temperature: float = 1.0,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        stream_start_timeout: float = 60.0,
        max_retries: int = 2,
        context_window: int | None = None,
        compact_threshold: float | None = None,
        reasoning_effort: str | None = None,
        supports_reasoning: bool | None = None,
        supports_reasoning_effort: bool = False,
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
        if not 0 <= temperature <= 1:
            raise ValueError("temperature must be between 0 and 1")
        if (
            provider in {"anthropic", "moonshotai", "minimax"}
            and reasoning_effort
            and reasoning_effort != "none"
            and temperature != 1.0
        ):
            raise ValueError(f"{provider} does not support custom temperature when thinking is enabled")
        self.temperature = float(temperature)
        self.compact_threshold = compact_threshold if compact_threshold is not None else DEFAULT_COMPACT_THRESHOLD
        self.reasoning_effort = reasoning_effort
        # Whether the endpoint accepts the effort knob. Comes from provider
        # config, not the catalog, so it stays out of refresh_capabilities.
        self.supports_reasoning_effort = supports_reasoning_effort

        self.system = system
        self.hooks = hooks or Hooks()
        self._cancel_event = asyncio.Event()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._provider_event_task: asyncio.Future[ProviderStreamEvent] | None = None
        self._active_tool_task: asyncio.Task[ToolExecutionResult] | None = None

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
            supports_reasoning=supports_reasoning,
            supports_image_input=supports_image_input,
            supports_pdf_input=supports_pdf_input,
        )

    def refresh_capabilities(
        self,
        *,
        max_tokens: int | None = None,
        context_window: int | None = None,
        supports_reasoning: bool | None = None,
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
            supports_reasoning=supports_reasoning,
            supports_image_input=supports_image_input,
            supports_pdf_input=supports_pdf_input,
        )
        self.max_tokens: int = meta.max_output_tokens or 16_384
        self.context_window: int = meta.context_window or 128_000
        self.model_pricing: dict[str, Any] | None = meta.pricing
        self.supports_reasoning: bool | None = meta.supports_reasoning
        self.supports_image_input: bool = bool(meta.supports_image_input)
        self.supports_pdf_input: bool = bool(meta.supports_pdf_input)

    def cancel(self) -> None:
        """Request cancellation of the in-flight turn."""

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        loop = self._event_loop
        if loop is not None and loop.is_running() and running_loop is not loop:
            loop.call_soon_threadsafe(self._cancel_in_loop)
            return
        self._cancel_in_loop()

    def _cancel_in_loop(self) -> None:
        self._cancel_event.set()
        if self._provider_event_task and not self._provider_event_task.done():
            self._provider_event_task.cancel()
        if self._active_tool_task and not self._active_tool_task.done():
            self._active_tool_task.cancel()

    def clear(self) -> None:
        """Drop the in-memory conversation history."""

        self.messages = []

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        ctx: ToolContext[Any],
    ) -> ToolExecutionResult:
        return await self.tools.aexecute(spec.name, args, ctx)

    async def _reject_tool_call(
        self,
        tool_use: dict[str, Any],
        message: str,
    ) -> AsyncIterator[Event]:
        """Report a tool call that must not reach a runner."""

        tool_id = str(tool_use.get("id") or "")
        args = tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {}
        yield Event(
            "tool_start",
            {
                "tool_call": {
                    "id": tool_id,
                    "name": str(tool_use.get("name") or ""),
                    "input": args,
                }
            },
        )
        yield self._error_done(tool_id, message)

    async def _run_tool_call(self, tool_use: dict[str, Any]) -> AsyncIterator[Event]:
        """Run one tool call and emit the standard tool events."""

        tool_id = str(tool_use.get("id") or "")
        name = str(tool_use.get("name") or "")
        raw_args = tool_use.get("input")
        args = raw_args if isinstance(raw_args, dict) else {}

        # Surface the tool to the UI before running hooks so the call is
        # always visible — even when a hook is awaiting a permission decision.
        yield Event("tool_start", {"tool_call": {"id": tool_id, "name": name, "input": args}})

        if self._cancel_event.is_set():
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
        try:
            result = await self.hooks.run_before_tool(hook_ctx)
        except Exception as exc:
            yield self._error_done(tool_id, f"error: tool hook failed: {exc}")
            return

        if result is not None:
            yield await self._finish_tool_call(tool_id, hook_ctx, result)
            return

        if self._cancel_event.is_set():
            yield self._error_done(tool_id, "error: cancelled")
            return

        if spec.streams_output:
            async for event in self._run_streaming_tool(
                tool_id=tool_id,
                spec=spec,
                args=args,
                hook_ctx=hook_ctx,
            ):
                yield event
            return

        try:
            ctx = self._ctx_for_call(tool_id)
            task = asyncio.create_task(self._execute_tool(spec, args, ctx))
            self._active_tool_task = task
            try:
                result = await task
            finally:
                if self._active_tool_task is task:
                    self._active_tool_task = None
        except asyncio.CancelledError:
            if self._cancel_event.is_set():
                yield self._error_done(tool_id, "error: cancelled")
                return
            raise
        except Exception as exc:  # pragma: no cover - defensive
            result = ToolExecutionResult(output=f"error: {exc}", is_error=True)

        yield await self._finish_tool_call(tool_id, hook_ctx, result)

    async def _run_streaming_tool(
        self,
        *,
        tool_id: str,
        spec: ToolSpec,
        args: dict[str, Any],
        hook_ctx: ToolHookContext[Any],
    ) -> AsyncIterator[Event]:
        """Run one streaming tool, forwarding ``tool_output`` events live."""

        output_buffer = _ToolOutputBuffer(asyncio.get_running_loop())

        def on_output(delta: str) -> None:
            output_buffer.append(delta)

        ctx = self._ctx_for_call(tool_id, emit=on_output)

        async def run_tool() -> ToolExecutionResult:
            return await self._execute_tool(spec, args, ctx)

        task = asyncio.create_task(run_tool())
        # The callback wakes the consumer even when cancellation happens
        # before the tool coroutine starts.
        task.add_done_callback(lambda _task: output_buffer.finish())
        self._active_tool_task = task
        normal_completion = False

        try:
            while True:
                output = await output_buffer.get()
                if output is None:
                    normal_completion = True
                    break
                if output and not self._cancel_event.is_set():
                    yield Event("tool_output", {"tool_use_id": tool_id, "output": output})

            try:
                result = await task
            except asyncio.CancelledError:
                if not self._cancel_event.is_set():
                    raise
                result = ToolExecutionResult(output="error: cancelled", is_error=True)
            except Exception as exc:  # pragma: no cover - defensive
                result = ToolExecutionResult(output=f"error: {exc}", is_error=True)

            if self._cancel_event.is_set():
                # A cancelled call keeps the tool's own final result (or this
                # synthesized error) and skips after_tool hooks.
                yield self._tool_done_event(tool_id, result)
                return

            yield await self._finish_tool_call(tool_id, hook_ctx, result)
        finally:
            if not normal_completion and not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            if self._active_tool_task is task:
                self._active_tool_task = None

    async def _finish_tool_call(
        self,
        tool_id: str,
        hook_ctx: ToolHookContext[Any],
        result: ToolExecutionResult,
    ) -> Event:
        try:
            result = await self.hooks.run_after_tool(hook_ctx, result)
        except Exception:
            logger.exception(
                "after_tool hook failed for %s (call %s)",
                hook_ctx.tool_name,
                hook_ctx.tool_call_id,
            )
        return self._tool_done_event(tool_id, result)

    def _ctx_for_call(
        self,
        tool_id: str,
        *,
        emit: Callable[[str], None] | None = None,
    ) -> ToolContext[Any]:
        return ToolContext(
            executor=self.tools,
            deps=self.deps,
            supports_image_input=self.supports_image_input,
            tool_call_id=tool_id,
            emit=emit,
        )

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
        adapter: ProviderAdapter,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
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
                async for event in self._stream_provider_attempt(adapter, request):
                    if event.type == "stream_started":
                        continue
                    if event.type in {"thinking_delta", "text_delta"}:
                        visible_output_emitted = True
                    yield event
                return
            except ProviderError as exc:
                if visible_output_emitted or not exc.retryable or attempt >= max_attempts:
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
                await self._backoff(delay)

    async def _stream_provider_attempt(
        self,
        adapter: ProviderAdapter,
        request: ProviderRequest,
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Iterate one provider attempt with cancellation and the start deadline.

        The deadline covers everything up to the first upstream event: DNS,
        connect, request upload, response headers, and the wait for the first
        SSE event or chunk.
        """

        provider_stream: AsyncIterator[ProviderStreamEvent] = adapter.stream_turn(request)
        started = False

        try:
            while True:
                if self._cancel_event.is_set():
                    raise asyncio.CancelledError

                self._provider_event_task = asyncio.ensure_future(anext(provider_stream))
                try:
                    if started:
                        event = await self._provider_event_task
                    else:
                        try:
                            # wait_for cancels the pending anext and awaits its
                            # cancellation before raising.
                            event = await asyncio.wait_for(self._provider_event_task, self.stream_start_timeout)
                        except TimeoutError:
                            raise StreamStartTimeoutError(
                                f"no provider stream event received within {self.stream_start_timeout:g}s"
                            ) from None
                except StopAsyncIteration:
                    return
                finally:
                    self._provider_event_task = None

                started = True
                yield event
        finally:
            # Runs before the retry loop backs off, so a failed attempt's
            # stream is closed before the next attempt starts.
            close = cast(Callable[[], Awaitable[None]] | None, getattr(provider_stream, "aclose", None))
            if close is not None:
                with suppress(Exception):
                    await close()

    def _retry_delay(self, failed_attempts: int, error: ProviderError) -> float:
        """Backoff before the next attempt: Retry-After when sane, else exponential."""

        if error.retry_after is not None and 0 < error.retry_after <= 60:
            return error.retry_after
        base = min(8.0, 0.5 * 2 ** (failed_attempts - 1))
        return base * (1 - 0.25 * random.random())

    async def _backoff(self, delay: float) -> None:
        """Wait between attempts; Agent.cancel() interrupts immediately."""

        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=delay)
        except TimeoutError:
            return
        raise asyncio.CancelledError

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
            temperature=self.temperature,
            api_key=self.api_key,
            api_base=self.api_base,
            reasoning_effort=reasoning_effort,
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
        partial_content: list[dict[str, Any]],
        duration_ms: int | None,
        *,
        stop_reason: str | None = None,
    ) -> ConversationMessage:
        """Build the assistant message persisted for an interrupted stream."""

        if duration_ms is not None:
            self._stamp_thinking_duration(partial_content, duration_ms)
        meta: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "context_window": self.context_window,
        }
        if stop_reason:
            meta["stop_reason"] = stop_reason
        return build_message("assistant", [dict(block) for block in partial_content], meta=meta)

    def _usage_event(
        self,
        context_tokens: int | None,
        turn_usage: dict[str, Any],
        turn_cost: Cost | None,
    ) -> Event:
        """Build a usage event: turn-cumulative billing facts + context metric."""

        return Event(
            "usage",
            {
                "context_tokens": context_tokens,
                "turn_usage": dict(turn_usage),
                "turn_cost": dict(turn_cost) if turn_cost is not None else None,
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

    async def _persist_message(
        self,
        message: ConversationMessage,
        on_persist: PersistCallback | None,
    ) -> None:
        """Persist one message: caller callback first, then the SDK session store."""

        if on_persist is not None:
            # Callers may need to write related records before the SDK
            # appends this message to its own session log.
            await on_persist(message)
        if self._store is None:
            return
        await self._store.append_message(self.session_id, message)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def achat(
        self,
        user_input: str | ConversationMessage,
        *,
        attachments: Sequence[AttachmentLike] = (),
        on_persist: PersistCallback | None = None,
    ) -> AsyncIterator[Event]:
        """Run the full agent loop for one user message."""

        async def persist(message: ConversationMessage) -> None:
            await self._persist_message(message, on_persist)

        self._event_loop = asyncio.get_running_loop()
        self._cancel_event.clear()

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
            blocks = await asyncio.to_thread(build_attachment_blocks, attachments)
            user_message["content"] = list(user_message.get("content") or []) + blocks

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

        self.messages.append(user_message)
        await persist(user_message)

        adapter = get_provider_adapter(self.provider)

        turn_usage: dict[str, Any] = {}
        turn_cost: Cost | None = None
        context_tokens: int | None = None
        turn_number = 0
        while True:
            if self.max_turns is not None and turn_number >= self.max_turns:
                yield Event("error", {"message": "max_turns reached"})
                return
            turn_number += 1
            if self._cancel_event.is_set():
                yield Event("error", {"message": "cancelled"})
                return

            assistant_message: ConversationMessage | None = None
            partial_content: list[dict[str, Any]] = []
            thinking_started_at: float | None = None
            thinking_duration_ms: int | None = None
            provider_cancelled = False
            request = self._build_request(reasoning_effort=self.reasoning_effort)

            try:
                async for provider_event in self._stream_provider_turn(adapter, request):
                    if self._cancel_event.is_set():
                        provider_cancelled = True
                        break

                    if provider_event.type == "retry":
                        yield Event("retry", dict(provider_event.data))
                        continue

                    if provider_event.type == "thinking_delta":
                        delta_text = str(provider_event.data.get("text") or "")
                        if delta_text:
                            if thinking_started_at is None:
                                thinking_started_at = time.monotonic()
                            if partial_content and partial_content[-1].get("type") == "thinking":
                                partial_content[-1]["text"] = f"{partial_content[-1].get('text') or ''}{delta_text}"
                            else:
                                partial_content.append(thinking_block(delta_text))
                            yield Event("reasoning", {"delta": delta_text})
                        continue

                    if provider_event.type == "text_delta":
                        delta_text = str(provider_event.data.get("text") or "")
                        if delta_text:
                            if thinking_started_at is not None and thinking_duration_ms is None:
                                thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                                yield Event("reasoning_done", {"duration_ms": thinking_duration_ms})
                            if partial_content and partial_content[-1].get("type") == "text":
                                partial_content[-1]["text"] = f"{partial_content[-1].get('text') or ''}{delta_text}"
                            else:
                                partial_content.append(text_block(delta_text))
                            yield Event("text", {"delta": delta_text})
                        continue

                    if provider_event.type != "message_done":
                        continue

                    if thinking_started_at is not None and thinking_duration_ms is None:
                        thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                        yield Event("reasoning_done", {"duration_ms": thinking_duration_ms})

                    message = provider_event.data.get("message")
                    if isinstance(message, dict):
                        assistant_message = message

            except asyncio.CancelledError:
                provider_cancelled = True
            except Exception as exc:
                logger.exception("Provider request failed")
                if partial_content:
                    # Output already reached the caller, so the attempt was not
                    # retried; keep the JSONL consistent with what was shown.
                    # stop_reason="error" excludes the partial from replay.
                    if thinking_started_at is not None and thinking_duration_ms is None:
                        thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                    failed_message = self._partial_assistant_message(
                        partial_content, thinking_duration_ms, stop_reason="error"
                    )
                    self.messages.append(failed_message)
                    await persist(failed_message)
                yield Event("error", {"message": str(exc)})
                return

            if provider_cancelled:
                if partial_content:
                    if thinking_started_at is not None and thinking_duration_ms is None:
                        thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                    cancelled_message = self._partial_assistant_message(
                        partial_content,
                        thinking_duration_ms,
                        stop_reason="cancelled",
                    )
                    self.messages.append(cancelled_message)
                    await persist(cancelled_message)
                yield Event("error", {"message": "cancelled"})
                return

            if not assistant_message:
                yield Event("error", {"message": "provider produced no assistant message"})
                return

            if thinking_duration_ms is not None:
                self._stamp_thinking_duration(assistant_message.get("content") or [], thinking_duration_ms)

            request_usage, request_cost = self._finalize_request_message(assistant_message)

            self.messages.append(assistant_message)
            await persist(assistant_message)

            context_tokens = request_usage.get("total_tokens")
            turn_cost = _accumulate_usage(turn_usage, turn_cost, request_usage, request_cost)
            yield self._usage_event(context_tokens, turn_usage, turn_cost)

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
                    block_meta = tool_call.get("meta") or {}
                    if stop_reason == "length":
                        events = self._reject_tool_call(
                            tool_call,
                            "error: tool call was truncated by the output token limit and was not executed",
                        )
                    elif block_meta.get("invalid_input") is True:
                        events = self._reject_tool_call(
                            tool_call,
                            "\n".join(
                                [
                                    f"error: invalid arguments for {tool_call.get('name')} and the call was not executed.",
                                    f"parse error: {block_meta.get('parse_error')}",
                                    f"raw arguments: {block_meta.get('raw_arguments')}",
                                ]
                            ),
                        )
                    elif stop_reason not in {"stop", "tool_use"}:
                        events = self._reject_tool_call(
                            tool_call,
                            "error: provider response did not complete the tool call and it was not executed",
                        )
                    else:
                        events = self._run_tool_call(tool_call)

                    async for event in events:
                        yield event

                        if event.type != "tool_done":
                            continue

                        d = event.data
                        tool_results.append(
                            tool_result_block(
                                tool_use_id=d["tool_use_id"],
                                output=d["output"],
                                metadata=d.get("metadata"),
                                is_error=d["is_error"],
                                content=d.get("content"),
                            )
                        )

                    if self._cancel_event.is_set():
                        # Skip remaining tool calls; the results collected so
                        # far are still persisted below.
                        break

                tool_result_message = build_message("user", tool_results)
                self.messages.append(tool_result_message)
                await persist(tool_result_message)

            if self._cancel_event.is_set():
                return
            if should_compact(context_tokens, self.context_window, self.compact_threshold):
                try:
                    compact_marker = await self._compact(adapter, on_persist)
                    yield Event("compact", {})
                    # The summary call is a billed provider request; its total
                    # is not the post-compact context size, so context_tokens
                    # keeps the last normal request's value.
                    compact_usage = cast(dict[str, Any], (compact_marker.get("meta") or {}).get("usage") or {})
                    compact_cost = cast(Cost | None, (compact_marker.get("meta") or {}).get("cost"))
                    turn_cost = _accumulate_usage(turn_usage, turn_cost, compact_usage, compact_cost)
                    yield self._usage_event(context_tokens, turn_usage, turn_cost)
                except asyncio.CancelledError:
                    yield Event("error", {"message": "cancelled"})
                    return
                except Exception:
                    # Compaction must not block the current answer; the full
                    # transcript is still available for the next turn.
                    logger.warning(
                        "Context compaction failed, continuing without compaction",
                        exc_info=True,
                    )

            if not tool_calls:
                return

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
            async for event in self.achat(user_input, attachments=attachments, on_persist=on_persist):
                if event.type != "tool_output":
                    result.events.append(event)
                if event.type == "text":
                    result.text += str(event.data.get("delta") or "")
                elif event.type == "usage":
                    result.usage = event.data
                elif event.type == "error" and result.error is None:
                    result.error = str(event.data.get("message") or "")
            return result

        return asyncio.run(collect())

    # ------------------------------------------------------------------
    # Context compaction
    # ------------------------------------------------------------------

    async def acompact(
        self,
        *,
        on_persist: PersistCallback | None = None,
    ) -> ConversationMessage:
        """Compact the conversation now and return the persisted compact marker.

        Raises :class:`NothingToCompactError` when no new context follows the
        latest compact marker, and :class:`asyncio.CancelledError` when
        :meth:`cancel` stops the summary request.
        """

        self._event_loop = asyncio.get_running_loop()
        self._cancel_event.clear()
        adapter = get_provider_adapter(self.provider)
        return await self._compact(adapter, on_persist)

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

    async def _compact(
        self,
        adapter: ProviderAdapter,
        on_persist: PersistCallback | None,
    ) -> ConversationMessage:
        """Ask the provider for a summary, persist and append the compact marker."""

        if not has_compactable_history(self.messages):
            raise NothingToCompactError("nothing to compact")

        request = self._build_request(
            tools=[],
            max_tokens=min(self.max_tokens, 8192),
            append_messages=[user_text_message(COMPACT_SUMMARY_PROMPT)],
        )

        summary_message: ConversationMessage | None = None
        async for provider_event in self._stream_provider_turn(adapter, request):
            if provider_event.type == "message_done":
                msg = provider_event.data.get("message")
                if isinstance(msg, dict):
                    summary_message = msg

        if not summary_message:
            raise ValueError("compaction produced no response")

        summary_text = flatten_message_text(summary_message, include_thinking=False)
        if not summary_text:
            raise ValueError("compaction produced empty summary")

        if self._cancel_event.is_set():
            raise asyncio.CancelledError

        summary_usage, summary_cost = self._finalize_request_message(summary_message)
        compact_event = build_compact_event(
            summary_text,
            provider=self.provider,
            model=self.model,
            context_window=self.context_window,
            usage=summary_usage,
            cost=summary_cost,
        )

        await self._persist_message(compact_event, on_persist)
        self.messages.append(compact_event)
        return compact_event
