"""Multi-turn agent loop.

:class:`Agent` drives one conversation. Each call to :meth:`Agent.achat`
runs one user turn; when a ``session_dir`` is configured, every emitted
message is appended to the on-disk session log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
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
    build_compact_event,
    should_compact,
)
from mycode.hooks import Hooks, ToolHookContext
from mycode.messages import (
    ConversationMessage,
    build_message,
    flatten_message_text,
    text_block,
    thinking_block,
    tool_result_block,
    user_text_message,
)
from mycode.models import infer_provider_from_model, resolve_model_metadata
from mycode.providers import get_provider_adapter
from mycode.providers.base import ProviderAdapter, ProviderRequest, ProviderStreamEvent
from mycode.session import SessionStore
from mycode.tools import ToolContext, ToolExecutionResult, ToolExecutor, ToolSpec

logger = logging.getLogger(__name__)

PersistCallback = Callable[[ConversationMessage], Awaitable[None]]


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


class Agent:
    """Multi-turn tool-calling agent runtime."""

    def __init__(
        self,
        *,
        model: str,
        cwd: str | None = None,
        provider: str | None = None,
        session_dir: Path | None = None,
        session_id: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        messages: list[ConversationMessage] | None = None,
        max_turns: int | None = None,
        max_tokens: int | None = None,
        temperature: float = 1.0,
        context_window: int | None = None,
        compact_threshold: float | None = None,
        reasoning_effort: str | None = None,
        supports_reasoning: bool | None = None,
        supports_image_input: bool | None = None,
        supports_pdf_input: bool | None = None,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        hooks: Hooks | None = None,
    ):
        self.model = model
        if provider is None:
            inferred = infer_provider_from_model(model)
            if inferred is None:
                raise ValueError(f"could not infer provider for model {model!r}; pass provider= explicitly")
            provider = inferred
        self.provider = provider

        resolved_cwd = cwd if cwd is not None else os.getcwd()
        self.cwd = str(Path(resolved_cwd).resolve(strict=False))

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

        self.system = system
        self.hooks = hooks or Hooks()
        self._cancel_event = asyncio.Event()
        self._provider_event_task: asyncio.Future[ProviderStreamEvent] | None = None

        # History resolution:
        # - messages is None → auto-resume from disk if the session exists
        # - messages is [] or [...] → use as-is; refuse if it would overwrite disk
        if messages is None:
            if self._store is not None:
                data = self._store.load_session_sync(self.session_id)
                messages = list(data["messages"]) if data is not None else []
            else:
                messages = []
        elif self._store is not None and self._store.session_exists(self.session_id):
            msg = (
                f"session {self.session_id!r} already exists on disk; "
                "pass messages=None to resume or choose a different session_id"
            )
            raise ValueError(msg)
        self.messages: list[ConversationMessage] = list(messages)

        # ``tool_output_dir`` defaults to a session-adjacent directory so logs
        # (e.g. bash spill files) live next to the session JSONL; without a
        # session, fall back to a tempdir scoped to ``session_id``.
        if session_dir is not None:
            self.tool_output_dir = session_dir / self.session_id / "tool-output"
        else:
            self.tool_output_dir = Path(tempfile.gettempdir()) / "mycode" / self.session_id / "tool-output"
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
        self.supports_reasoning: bool | None = meta.supports_reasoning
        self.supports_image_input: bool = bool(meta.supports_image_input)
        self.supports_pdf_input: bool = bool(meta.supports_pdf_input)

    def cancel(self) -> None:
        """Request cancellation of the in-flight turn."""

        self._cancel_event.set()
        self.tools.cancel_active()
        if self._provider_event_task and not self._provider_event_task.done():
            self._provider_event_task.cancel()

    def clear(self) -> None:
        """Drop the in-memory conversation history."""

        self.messages = []

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

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
            cwd=self.cwd,
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
            async for event in self._run_streaming_tool(tool_id=tool_id, name=name, args=args, hook_ctx=hook_ctx):
                yield event
            return

        try:
            ctx = self._ctx_for_call(tool_id)
            result = await asyncio.to_thread(self.tools.execute, name, args, ctx)
        except Exception as exc:  # pragma: no cover - defensive
            result = ToolExecutionResult(output=f"error: {exc}", is_error=True)

        yield await self._finish_tool_call(tool_id, hook_ctx, result)

    async def _run_streaming_tool(
        self,
        *,
        tool_id: str,
        name: str,
        args: dict[str, Any],
        hook_ctx: ToolHookContext,
    ) -> AsyncIterator[Event]:
        """Run one streaming tool, forwarding ``tool_output`` events live."""

        loop = asyncio.get_running_loop()
        output_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def on_output(line: str) -> None:
            loop.call_soon_threadsafe(output_queue.put_nowait, line)

        ctx = self._ctx_for_call(tool_id, emit=on_output)

        async def run_in_thread() -> ToolExecutionResult:
            try:
                return await asyncio.to_thread(self.tools.execute, name, args, ctx)
            finally:
                loop.call_soon_threadsafe(output_queue.put_nowait, None)

        task = asyncio.create_task(run_in_thread())
        was_cancelled = False
        output_parts: list[str] = []

        while True:
            if self._cancel_event.is_set() and not was_cancelled:
                was_cancelled = True
                self.tools.cancel_active()

            try:
                output = await asyncio.wait_for(output_queue.get(), timeout=0.1)
            except TimeoutError:
                if task.done():
                    break
                continue

            if output is None:
                break
            if not was_cancelled:
                output_parts.append(output)
                yield Event("tool_output", {"tool_use_id": tool_id, "output": output})

        if was_cancelled:
            with suppress(Exception):
                await task
            output = "\n".join([*output_parts, "error: cancelled"]) if output_parts else "error: cancelled"
            yield self._error_done(tool_id, output)
            return

        try:
            result = await task
        except Exception as exc:  # pragma: no cover - defensive
            result = ToolExecutionResult(output=f"error: {exc}", is_error=True)

        yield await self._finish_tool_call(tool_id, hook_ctx, result)

    async def _finish_tool_call(
        self,
        tool_id: str,
        hook_ctx: ToolHookContext,
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
    ) -> ToolContext:
        return ToolContext(
            executor=self.tools,
            cwd=self.cwd,
            tool_output_dir=self.tool_output_dir,
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
        """Iterate one provider turn with best-effort cancellation support."""

        provider_stream: AsyncIterator[ProviderStreamEvent] = adapter.stream_turn(request)

        try:
            while True:
                if self._cancel_event.is_set():
                    raise asyncio.CancelledError

                self._provider_event_task = asyncio.ensure_future(anext(provider_stream))
                try:
                    yield await self._provider_event_task
                except StopAsyncIteration:
                    return
                finally:
                    self._provider_event_task = None
        finally:
            close = cast(Callable[[], Awaitable[None]] | None, getattr(provider_stream, "aclose", None))
            if close is not None:
                with suppress(Exception):
                    await close()

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
            if on_persist is not None:
                # Callers may need to write related records before the SDK
                # appends this message to its own session log.
                await on_persist(message)
            if self._store is None:
                return
            if not self._store.session_exists(self.session_id):
                await self._store.create_session(self.session_id, cwd=self.cwd)
            await self._store.append_message(self.session_id, message)

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
            blocks = await asyncio.to_thread(build_attachment_blocks, attachments, cwd=self.cwd)
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

                    if provider_event.type == "provider_error":
                        raise ValueError(str(provider_event.data.get("message") or "provider error"))

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
                yield Event("error", {"message": str(exc)})
                return

            if provider_cancelled:
                if partial_content:
                    if thinking_started_at is not None and thinking_duration_ms is None:
                        thinking_duration_ms = self._elapsed_ms(thinking_started_at)
                    if thinking_duration_ms is not None:
                        self._stamp_thinking_duration(partial_content, thinking_duration_ms)
                    cancelled_message = build_message(
                        "assistant",
                        [dict(block) for block in partial_content],
                        meta={
                            "provider": self.provider,
                            "model": self.model,
                            "context_window": self.context_window,
                        },
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

            # Stamp context_window onto the persisted assistant message so
            # rewinds and refreshed clients can render token-usage % without
            # re-resolving model metadata.
            meta = cast(dict[str, Any], assistant_message.setdefault("meta", {}))
            meta["context_window"] = self.context_window

            self.messages.append(assistant_message)
            await persist(assistant_message)

            total_tokens = meta.get("total_tokens")
            if total_tokens:
                yield Event(
                    "usage",
                    {
                        "total_tokens": total_tokens,
                        "model": meta.get("model") or self.model,
                        "provider": meta.get("provider") or self.provider,
                        "context_window": meta["context_window"],
                    },
                )

            tool_calls = [
                block
                for block in assistant_message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if tool_calls:
                tool_results: list[dict[str, Any]] = []
                for tool_call in tool_calls:
                    async for event in self._run_tool_call(tool_call):
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
            if should_compact(total_tokens, self.context_window, self.compact_threshold):
                try:
                    await self._compact(adapter, persist)
                    yield Event("compact", {})
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
                result.events.append(event)
                if event.type == "text":
                    result.text += str(event.data.get("delta") or "")
                elif event.type == "error" and result.error is None:
                    result.error = str(event.data.get("message") or "")
            return result

        return asyncio.run(collect())

    # ------------------------------------------------------------------
    # Context compaction
    # ------------------------------------------------------------------

    async def _compact(
        self,
        adapter: ProviderAdapter,
        persist: PersistCallback,
    ) -> None:
        """Ask the provider for a summary, persist the compact event, append it."""

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

        summary_total_tokens = (summary_message.get("meta") or {}).get("total_tokens")
        compact_event = build_compact_event(
            summary_text,
            provider=self.provider,
            model=self.model,
            total_tokens=summary_total_tokens,
        )

        await persist(compact_event)
        self.messages.append(compact_event)
