"""Agent loop with provider-specific adapters and one internal message model."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mycode.core.config import Settings, get_settings
from mycode.core.messages import (
    ConversationMessage,
    build_message,
    flatten_message_text,
    tool_result_block,
    user_text_message,
)
from mycode.core.providers import get_provider_adapter
from mycode.core.providers.base import ProviderAdapter, ProviderRequest, ProviderStreamEvent
from mycode.core.session import (
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_COMPACT_THRESHOLD,
    apply_compact,
    build_compact_event,
    should_compact,
)
from mycode.core.system_prompt import build_system_prompt
from mycode.core.tools import ToolExecutionResult, ToolExecutor

logger = logging.getLogger(__name__)

PersistCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Event:
    """Streaming event emitted by the agent."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


class Agent:
    """Minimal coding agent with one internal loop and provider adapters."""

    def __init__(
        self,
        *,
        model: str,
        cwd: str,
        session_dir: Path,
        session_id: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_turns: int | None = None,
        max_tokens: int = 16_384,
        context_window: int | None = 128_000,
        compact_threshold: float | None = None,
        reasoning_effort: str | None = None,
        supports_image_input: bool | None = None,
        settings: Settings | None = None,
        system: str | None = None,
        tool_executor: ToolExecutor | None = None,
    ):
        self.model = model
        self.provider = provider or "anthropic"
        self.cwd = str(Path(cwd).resolve(strict=False))
        self.session_dir = session_dir
        self.session_id = (session_id or session_dir.name).strip() or None
        self.api_key = api_key
        self.api_base = api_base
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.context_window = context_window
        self.compact_threshold = compact_threshold if compact_threshold is not None else DEFAULT_COMPACT_THRESHOLD
        self.reasoning_effort = reasoning_effort
        self.supports_image_input: bool = bool(supports_image_input)
        self.settings = settings or get_settings(self.cwd)
        self.system = system or build_system_prompt(self.cwd, self.settings)
        self._cancel_event = asyncio.Event()
        self._provider_event_task: asyncio.Future[ProviderStreamEvent] | None = None

        self.messages: list[ConversationMessage] = list(messages or [])
        self.tools = tool_executor or ToolExecutor(cwd=self.cwd, session_dir=self.session_dir)
        self.tools.supports_image_input = self.supports_image_input

    def cancel(self) -> None:
        self._cancel_event.set()
        self.tools.cancel_active()
        if self._provider_event_task and not self._provider_event_task.done():
            self._provider_event_task.cancel()

    def clear(self) -> None:
        self.messages = []

    @staticmethod
    def _tool_done_event(tool_id: str, result: ToolExecutionResult) -> Event:
        """Build the standard tool_done event payload."""

        data = {
            "tool_use_id": tool_id,
            "model_text": result.model_text,
            "display_text": result.display_text,
            "is_error": result.is_error,
        }
        if result.content:
            data["content"] = result.content
        return Event(
            "tool_done",
            data,
        )

    async def _run_streaming_tool(self, *, tool_id: str, name: str, args: dict[str, Any]) -> AsyncIterator[Event]:
        """Run one streaming tool and forward live output until it finishes."""

        loop = asyncio.get_running_loop()
        output_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def on_output(line: str) -> None:
            loop.call_soon_threadsafe(output_queue.put_nowait, line)

        async def run_in_thread() -> ToolExecutionResult:
            try:
                return await asyncio.to_thread(
                    self.tools.run_streaming,
                    name,
                    tool_call_id=tool_id,
                    args=args,
                    on_output=on_output,
                )
            finally:
                loop.call_soon_threadsafe(output_queue.put_nowait, None)

        task = asyncio.create_task(run_in_thread())
        was_cancelled = False

        # Streaming tools produce intermediate output, but they still end as one
        # normal tool_done event so the outer loop can handle all tools the same way.
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
                yield Event("tool_output", {"tool_use_id": tool_id, "output": output})

        if was_cancelled:
            try:
                await task
            except Exception:
                pass
            result = ToolExecutionResult(
                model_text="error: cancelled",
                display_text="Cancelled",
                is_error=True,
            )
        else:
            try:
                result = await task
            except Exception as exc:  # pragma: no cover - defensive
                result = ToolExecutionResult(
                    model_text=f"error: {exc}",
                    display_text=str(exc),
                    is_error=True,
                )

        yield self._tool_done_event(tool_id, result)

    async def _run_tool_call(self, tool_use: dict[str, Any]) -> AsyncIterator[Event]:
        """Run one tool call and emit the standard tool events."""

        tool_id = str(tool_use.get("id") or "")
        name = str(tool_use.get("name") or "")
        raw_args = tool_use.get("input")
        args = raw_args if isinstance(raw_args, dict) else {}

        yield Event("tool_start", {"tool_call": {"id": tool_id, "name": name, "input": args}})

        if self._cancel_event.is_set():
            yield self._tool_done_event(
                tool_id,
                ToolExecutionResult(
                    model_text="error: cancelled",
                    display_text="Cancelled",
                    is_error=True,
                ),
            )
            return

        tool = self.tools.get_tool(name)
        if tool is None:
            yield self._tool_done_event(
                tool_id,
                ToolExecutionResult(
                    model_text=f"error: unknown tool: {name}",
                    display_text=f"Unknown tool: {name}",
                    is_error=True,
                ),
            )
            return

        if tool.streams_output:
            async for event in self._run_streaming_tool(tool_id=tool_id, name=name, args=args):
                yield event
            return

        try:
            result = self.tools.run(name, args=args)
        except Exception as exc:  # pragma: no cover - defensive
            result = ToolExecutionResult(
                model_text=f"error: {exc}",
                display_text=str(exc),
                is_error=True,
            )

        yield self._tool_done_event(tool_id, result)

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

                self._provider_event_task = asyncio.create_task(anext(provider_stream))
                try:
                    yield await self._provider_event_task
                except StopAsyncIteration:
                    return
                finally:
                    self._provider_event_task = None
        finally:
            close = getattr(provider_stream, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass

    async def achat(
        self,
        user_input: str | ConversationMessage,
        *,
        on_persist: PersistCallback | None = None,
    ) -> AsyncIterator[Event]:
        """Run the full agent loop for one user message.

        Each turn asks the provider for one assistant message. If the assistant
        requests tools, the agent runs them locally, appends one user-side
        tool_result message, and continues until the assistant stops using tools.
        """

        self._cancel_event.clear()
        supports_image_input = self.supports_image_input
        self.tools.supports_image_input = supports_image_input

        if isinstance(user_input, str):
            user_message = user_text_message(user_input)
        else:
            user_message = {
                "role": str(user_input.get("role") or "user"),
                "content": [dict(b) for b in user_input.get("content") or [] if isinstance(b, dict)],
            }
            if isinstance(user_input.get("meta"), dict):
                user_message["meta"] = dict(user_input["meta"])

        if user_message.get("role") != "user":
            yield Event("error", {"message": "user input must be a user message"})
            return

        if not supports_image_input and any(
            isinstance(block, dict) and block.get("type") == "image" for block in user_message.get("content") or []
        ):
            yield Event("error", {"message": "current model does not support image input"})
            return

        self.messages.append(user_message)
        if on_persist:
            await on_persist(user_message)

        adapter = get_provider_adapter(self.provider)

        turn_number = 0
        while self.max_turns is None or turn_number < self.max_turns:
            turn_number += 1
            if self._cancel_event.is_set():
                yield Event("error", {"message": "cancelled"})
                return

            assistant_message: ConversationMessage | None = None
            request = ProviderRequest(
                provider=self.provider,
                model=self.model,
                session_id=self.session_id,
                messages=self.messages,
                system=self.system,
                tools=self.tools.definitions,
                max_tokens=self.max_tokens,
                api_key=self.api_key,
                api_base=self.api_base,
                reasoning_effort=self.reasoning_effort,
                supports_image_input=supports_image_input,
            )

            try:
                # Phase 1: ask the provider for exactly one assistant turn.
                async for provider_event in self._stream_provider_turn(adapter, request):
                    if self._cancel_event.is_set():
                        yield Event("error", {"message": "cancelled"})
                        return

                    if provider_event.type == "thinking_delta":
                        delta_text = str(provider_event.data.get("text") or "")
                        if delta_text:
                            yield Event("reasoning", {"delta": delta_text})
                        continue

                    if provider_event.type == "text_delta":
                        delta_text = str(provider_event.data.get("text") or "")
                        if delta_text:
                            yield Event("text", {"delta": delta_text})
                        continue

                    if provider_event.type == "provider_error":
                        raise ValueError(str(provider_event.data.get("message") or "provider error"))

                    if provider_event.type != "message_done":
                        continue

                    message = provider_event.data.get("message")
                    if isinstance(message, dict):
                        assistant_message = message

            except asyncio.CancelledError:
                yield Event("error", {"message": "cancelled"})
                return
            except Exception as exc:
                logger.exception("Provider request failed")
                yield Event("error", {"message": str(exc)})
                return

            if not assistant_message:
                yield Event("error", {"message": "provider produced no assistant message"})
                return

            self.messages.append(assistant_message)
            if on_persist:
                await on_persist(assistant_message)

            # Phase 2: if the assistant requested tools, execute them locally and
            # append one user-side tool_result message before continuing.
            tool_calls = [
                block
                for block in assistant_message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if not tool_calls:
                break

            tool_results: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                async for event in self._run_tool_call(tool_call):
                    yield event

                    if event.type != "tool_done":
                        continue

                    d = event.data
                    model_text = str(d.get("model_text") or "")
                    content = d.get("content")
                    tool_results.append(
                        tool_result_block(
                            tool_use_id=str(d.get("tool_use_id") or ""),
                            model_text=model_text,
                            display_text=str(d.get("display_text") or ""),
                            is_error=bool(d.get("is_error")),
                            content=content if isinstance(content, list) else None,
                        )
                    )

                    if model_text == "error: cancelled" and self._cancel_event.is_set():
                        tool_result_message = build_message("user", tool_results)
                        self.messages.append(tool_result_message)
                        if on_persist:
                            await on_persist(tool_result_message)
                        return

            tool_result_message = build_message("user", tool_results)
            self.messages.append(tool_result_message)
            if on_persist:
                await on_persist(tool_result_message)

        else:
            # while loop exhausted max_turns without breaking
            yield Event("error", {"message": "max_turns reached"})
            return

        # Turn completed normally (assistant stopped calling tools).
        # Check whether context compaction is needed.
        if not self._cancel_event.is_set():
            async for event in self._compact_if_needed(adapter, on_persist):
                yield event

    # -----------------------------------------------------------------
    # Context compaction
    # -----------------------------------------------------------------

    async def _compact_if_needed(
        self,
        adapter: ProviderAdapter,
        on_persist: PersistCallback | None,
    ) -> AsyncIterator[Event]:
        """Check token usage and run compaction if above threshold."""

        usage: dict[str, Any] | None = None
        for message in reversed(self.messages):
            if message.get("role") == "assistant":
                usage = (message.get("meta") or {}).get("usage")
                break

        if not should_compact(usage, self.context_window, self.compact_threshold):
            return

        try:
            async for event in self._compact(adapter, on_persist):
                yield event
        except (Exception, asyncio.CancelledError):
            logger.warning("Context compaction failed, continuing without compaction", exc_info=True)

    async def _compact(
        self,
        adapter: ProviderAdapter,
        on_persist: PersistCallback | None,
    ) -> AsyncIterator[Event]:
        """Generate a conversation summary and replace in-memory messages."""
        compacted_count = len(self.messages)

        # Ask the same provider for a summary — no tools, just text generation.
        compact_messages = list(self.messages) + [user_text_message(COMPACT_SUMMARY_PROMPT)]
        request = ProviderRequest(
            provider=self.provider,
            model=self.model,
            session_id=self.session_id,
            messages=compact_messages,
            system=self.system,
            tools=[],
            max_tokens=min(self.max_tokens, 8192),
            api_key=self.api_key,
            api_base=self.api_base,
            supports_image_input=self.supports_image_input,
        )

        summary_message: ConversationMessage | None = None
        async for provider_event in self._stream_provider_turn(adapter, request):
            if provider_event.type == "message_done":
                msg = provider_event.data.get("message")
                if isinstance(msg, dict):
                    summary_message = msg

        if not summary_message:
            logger.warning("Compaction produced no response")
            return

        summary_text = flatten_message_text(summary_message, include_thinking=False)
        if not summary_text:
            logger.warning("Compaction produced empty summary")
            return

        summary_usage = (summary_message.get("meta") or {}).get("usage")
        compact_event = build_compact_event(
            summary_text,
            provider=self.provider,
            model=self.model,
            compacted_count=compacted_count,
            usage=summary_usage,
        )

        # Persist the compact event (append-only — original messages stay in JSONL).
        if on_persist:
            await on_persist(compact_event)

        # Rebuild in-memory messages from the compact event.
        self.messages.append(compact_event)
        self.messages = apply_compact(self.messages)

        yield Event(
            "compact",
            {
                "message": f"Context compacted ({compacted_count} messages → summary)",
                "compacted_count": compacted_count,
            },
        )
