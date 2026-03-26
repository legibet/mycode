"""Agent loop with provider-specific adapters and one internal message model."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mycode.core.compact import (
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_COMPACT_THRESHOLD,
    apply_compact,
    build_compact_event,
    should_compact,
)
from mycode.core.config import Settings, get_settings
from mycode.core.instructions import load_instructions_prompt
from mycode.core.messages import (
    ConversationMessage,
    build_message,
    flatten_message_text,
    tool_result_block,
    user_text_message,
)
from mycode.core.providers import get_provider_adapter
from mycode.core.providers.base import ProviderAdapter, ProviderRequest, ProviderStreamEvent
from mycode.core.skills import load_skills_prompt
from mycode.core.tools import ToolExecutor

logger = logging.getLogger(__name__)

PersistCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Event:
    """Streaming event emitted by the agent."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


async def _run_streaming_tool_to_queue(
    tools: ToolExecutor,
    *,
    name: str,
    tool_call_id: str,
    args: dict[str, Any],
    queue: asyncio.Queue[str | None],
    on_output: Callable[[str], None],
) -> str:
    """Run one streaming tool in a worker thread and signal completion."""

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.to_thread(
            tools.run_streaming,
            name,
            tool_call_id=tool_call_id,
            args=args,
            on_output=on_output,
        )
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


def _load_system_prompt() -> str:
    path = Path(__file__).resolve().parent / "system_prompt.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return "You are mycode, an expert coding assistant."


def build_system_prompt(cwd: str, settings: Settings | None = None) -> str:
    """Build the default runtime system prompt for a workspace."""

    resolved_cwd = str(Path(cwd).resolve(strict=False))
    resolved_settings = settings or get_settings(resolved_cwd)
    prompt_parts = [_load_system_prompt()]

    instructions_prompt = load_instructions_prompt(resolved_cwd, resolved_settings)
    skills_prompt = load_skills_prompt(resolved_cwd)
    if instructions_prompt:
        prompt_parts.append(instructions_prompt)
    if skills_prompt:
        prompt_parts.append(skills_prompt)
    prompt_parts.append(f"Current working directory: {resolved_cwd}")
    return "\n\n".join(prompt_parts)


def _extract_last_usage(messages: list[ConversationMessage]) -> dict[str, Any] | None:
    """Return the usage dict from the last assistant message, if available."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return (msg.get("meta") or {}).get("usage")
    return None


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
        max_tokens: int = 8192,
        context_window: int | None = None,
        compact_threshold: float | None = None,
        reasoning_effort: str | None = None,
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
        self.settings = settings or get_settings(self.cwd)
        self.system = system or build_system_prompt(self.cwd, self.settings)
        self._cancel_event = asyncio.Event()
        self._provider_event_task: asyncio.Future[ProviderStreamEvent] | None = None

        self.messages: list[ConversationMessage] = list(messages or [])
        self.tools = tool_executor or ToolExecutor(cwd=self.cwd, session_dir=self.session_dir)

    def cancel(self) -> None:
        self._cancel_event.set()
        self.tools.cancel_active()
        if self._provider_event_task and not self._provider_event_task.done():
            self._provider_event_task.cancel()

    def clear(self) -> None:
        self.messages = []

    async def _run_streaming_tool(self, *, tool_id: str, name: str, args: dict[str, Any]) -> AsyncIterator[Event]:
        """Stream tool output while a streaming tool is still running."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def on_output(
            line: str,
            _loop: asyncio.AbstractEventLoop = loop,
            _queue: asyncio.Queue[str | None] = queue,
        ) -> None:
            _loop.call_soon_threadsafe(_queue.put_nowait, line)

        task = asyncio.create_task(
            _run_streaming_tool_to_queue(
                self.tools,
                name=name,
                tool_call_id=tool_id,
                args=args,
                queue=queue,
                on_output=on_output,
            )
        )

        cancelled = False
        while True:
            if self._cancel_event.is_set() and not cancelled:
                cancelled = True
                self.tools.cancel_active()

            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                if task.done():
                    break
                continue

            if item is None:
                break

            if not cancelled:
                yield Event("tool_output", {"tool_use_id": tool_id, "output": item})

        if cancelled:
            try:
                await task
            except Exception:
                pass
            result = "error: cancelled"
        else:
            result = await task

        yield Event(
            "tool_done",
            {
                "tool_use_id": tool_id,
                "result": result,
                "is_error": result.startswith("error:"),
            },
        )

    async def _run_tool_call(self, tool_use: dict[str, Any]) -> AsyncIterator[Event]:
        """Run one tool call and emit the standard tool events."""

        tool_id = str(tool_use.get("id") or "")
        name = str(tool_use.get("name") or "")
        raw_args = tool_use.get("input")
        args = raw_args if isinstance(raw_args, dict) else {}

        yield Event("tool_start", {"tool_call": {"id": tool_id, "name": name, "input": args}})

        if self._cancel_event.is_set():
            yield Event(
                "tool_done",
                {"tool_use_id": tool_id, "result": "error: cancelled", "is_error": True},
            )
            return

        tool = self.tools.get_tool(name)
        if tool is None:
            yield Event(
                "tool_done",
                {"tool_use_id": tool_id, "result": f"error: unknown tool: {name}", "is_error": True},
            )
            return

        try:
            if tool.streams_output:
                async for event in self._run_streaming_tool(tool_id=tool_id, name=name, args=args):
                    yield event
                return
            result = self.tools.run(name, args=args)
        except Exception as exc:  # pragma: no cover - defensive
            result = f"error: {exc}"

        yield Event(
            "tool_done",
            {
                "tool_use_id": tool_id,
                "result": result,
                "is_error": result.startswith("error:"),
            },
        )

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

                next_event = cast(Awaitable[ProviderStreamEvent], anext(provider_stream))
                self._provider_event_task = asyncio.ensure_future(next_event)
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
                    await cast(Callable[[], Awaitable[Any]], close)()
                except Exception:
                    pass

    async def achat(self, user_input: str, *, on_persist: PersistCallback | None = None) -> AsyncIterator[Event]:
        """Run the full agent loop for one user message.

        Each turn asks the provider for one assistant message. If the assistant
        requests tools, the agent runs them locally, appends one user-side
        tool_result message, and continues until the assistant stops using tools.
        """

        self._cancel_event.clear()

        user_message = user_text_message(user_input)
        self.messages.append(user_message)
        if on_persist:
            await on_persist(user_message)

        adapter = get_provider_adapter(self.provider)

        turn_count = 0
        while self.max_turns is None or turn_count < self.max_turns:
            turn_count += 1
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
            )

            try:
                # Phase 1: ask the provider for exactly one assistant turn.
                async for provider_event in self._stream_provider_turn(adapter, request):
                    if self._cancel_event.is_set():
                        yield Event("error", {"message": "cancelled"})
                        return

                    if provider_event.type == "thinking_delta":
                        text = str(provider_event.data.get("text") or "")
                        if text:
                            yield Event("reasoning", {"delta": text})
                        continue

                    if provider_event.type == "text_delta":
                        text = str(provider_event.data.get("text") or "")
                        if text:
                            yield Event("text", {"delta": text})
                        continue

                    if provider_event.type == "message_done":
                        message = provider_event.data.get("message")
                        if isinstance(message, dict):
                            assistant_message = message
                        continue

                    if provider_event.type == "provider_error":
                        raise ValueError(str(provider_event.data.get("message") or "provider error"))

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
            tool_uses = [
                block
                for block in assistant_message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if not tool_uses:
                break

            tool_results: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                async for event in self._run_tool_call(tool_use):
                    yield event

                    if event.type != "tool_done":
                        continue

                    tool_id = str(event.data.get("tool_use_id") or "")
                    result = str(event.data.get("result") or "")
                    is_error = bool(event.data.get("is_error"))
                    tool_results.append(tool_result_block(tool_use_id=tool_id, content=result, is_error=is_error))

                    if result == "error: cancelled" and self._cancel_event.is_set():
                        user_tool_result = build_message("user", tool_results)
                        self.messages.append(user_tool_result)
                        if on_persist:
                            await on_persist(user_tool_result)
                        return

            user_tool_result = build_message("user", tool_results)
            self.messages.append(user_tool_result)
            if on_persist:
                await on_persist(user_tool_result)

        else:
            # while loop exhausted max_turns without breaking
            yield Event("error", {"message": "max_turns reached"})
            return

        # Turn completed normally (assistant stopped calling tools).
        # Check whether context compaction is needed.
        if not self._cancel_event.is_set():
            async for event in self._maybe_compact(adapter, on_persist):
                yield event

    # -----------------------------------------------------------------
    # Context compaction
    # -----------------------------------------------------------------

    async def _maybe_compact(
        self,
        adapter: ProviderAdapter,
        on_persist: PersistCallback | None,
    ) -> AsyncIterator[Event]:
        """Check token usage and run compaction if above threshold."""
        usage = _extract_last_usage(self.messages)
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
