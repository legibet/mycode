"""Agent loop (minimal, pi-inspired).

Key goals:
- Keep the agent loop small and predictable.
- Only 4 tools: read/write/edit/bash.
- Streaming output via SSE-friendly events.
- Low token overhead via truncation and minimal prompt.

This agent stores messages in OpenAI-style message dicts:
- {role: 'user', content: '...'}
- {role: 'assistant', content: '...', tool_calls: [...]}
- {role: 'tool', tool_call_id: '...', content: '...'}

The system prompt is loaded from system_prompt.md and is NOT persisted in sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from any_llm import acompletion

from app.agent.instructions import load_instructions_prompt
from app.agent.skills import load_skills_prompt
from app.agent.tools import TOOLS, ToolExecutor, cancel_all_tools, parse_tool_arguments
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


PersistCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Event:
    """Streaming event emitted by the agent."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallBuffer:
    """Accumulates a single tool call during streaming."""

    index: int
    id: str | None = None
    name: str = ""
    arguments: str = ""
    # Set True once arguments form valid JSON; extra deltas from misbehaving
    # proxies are then silently dropped rather than corrupting the stored value.
    _args_complete: bool = field(default=False, repr=False)


async def _run_bash_to_queue(
    tools: ToolExecutor,
    *,
    tool_call_id: str,
    command: str,
    timeout: Any,
    queue: asyncio.Queue[str | None],
    on_output: Callable[[str], None],
) -> str:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.to_thread(
            tools.bash,
            tool_call_id=tool_call_id,
            command=command,
            timeout=timeout,
            on_output=on_output,
        )
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


def _load_system_prompt() -> str:
    path = Path(__file__).resolve().parent / "system_prompt.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        # Fallback to a tiny prompt if file is missing
        return "You are a minimal coding assistant. Be concise."


class Agent:
    """Minimal coding agent with tool calling."""

    def __init__(
        self,
        *,
        model: str,
        cwd: str,
        session_dir: Path,
        provider: str | None = None,  # any_llm provider type e.g. "openai", "anthropic", "gemini"
        api_key: str | None = None,
        api_base: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_turns: int = 20,
        max_tokens: int = 8192,
        settings: Settings | None = None,
    ):
        self.model = model
        self.provider = provider
        self.cwd = str(Path(cwd).resolve(strict=False))
        self.session_dir = session_dir
        self.api_key = api_key
        self.api_base = api_base

        self.max_turns = max_turns
        self.max_tokens = max_tokens

        self.settings = settings or get_settings(self.cwd)
        self._system_prompt = _load_system_prompt()
        self._instructions_prompt = load_instructions_prompt(self.cwd, self.settings)
        self._skills_prompt = load_skills_prompt(self.cwd)
        self._cancel_event = asyncio.Event()

        self.messages: list[dict[str, Any]] = []
        self._init_messages(messages or [])

        self.tools = ToolExecutor(cwd=self.cwd, session_dir=self.session_dir)

    def _init_messages(self, persisted_messages: list[dict[str, Any]]) -> None:
        """Initialize messages (system prompt + persisted conversation)."""

        parts = [self._system_prompt]
        if self._instructions_prompt:
            parts.append(self._instructions_prompt)
        if self._skills_prompt:
            parts.append(self._skills_prompt)
        parts.append(f"Current working directory: {self.cwd}")
        system = {
            "role": "system",
            "content": "\n\n".join(parts),
        }
        self.messages = [system]
        self.messages.extend(persisted_messages)

        # If a previous run crashed mid-tool-use, ensure tool results exist.
        self._finalize_pending_tool_calls()

    def _finalize_pending_tool_calls(self) -> None:
        """Ensure every assistant tool_call has a corresponding tool result.

        This prevents providers from rejecting the next request.
        """

        # Find the last assistant message containing tool_calls.
        last_idx: int | None = None
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                last_idx = i
                break

        if last_idx is None:
            return

        tool_calls = self.messages[last_idx].get("tool_calls") or []
        tool_ids = [tc.get("id") for tc in tool_calls if tc.get("id")]
        if not tool_ids:
            return

        seen: set[str] = set()
        for msg in self.messages[last_idx + 1 :]:
            if msg.get("role") == "tool" and msg.get("tool_call_id") in tool_ids:
                seen.add(msg["tool_call_id"])

        missing = [tool_id for tool_id in tool_ids if tool_id not in seen]
        for tool_id in missing:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": "error: tool call was interrupted (no result recorded)",
                }
            )

    def cancel(self) -> None:
        """Request cancellation of the current run."""

        self._cancel_event.set()

    def clear(self) -> None:
        """Clear conversation (keeps system prompt)."""

        self._init_messages([])

    async def _persist_message(self, message: dict[str, Any], on_persist: PersistCallback | None) -> None:
        self.messages.append(message)
        if on_persist:
            await on_persist(message)

    async def _record_tool_result(
        self,
        *,
        tool_id: str,
        result: str,
        on_persist: PersistCallback | None,
    ) -> None:
        tool_msg = {"role": "tool", "tool_call_id": tool_id, "content": result}
        await self._persist_message(tool_msg, on_persist)

    async def achat(self, user_input: str, *, on_persist: PersistCallback | None = None) -> AsyncIterator[Event]:
        """Run the agent loop for a single user message, yielding streaming events."""

        self._cancel_event.clear()

        # Append user message
        user_msg = {"role": "user", "content": user_input}
        await self._persist_message(user_msg, on_persist)

        for _turn in range(self.max_turns):
            if self._cancel_event.is_set():
                yield Event("error", {"message": "cancelled"})
                return

            # Request LLM streaming completion
            try:
                stream = cast(
                    AsyncIterator[Any],
                    await acompletion(
                        model=self.model,
                        provider=self.provider,
                        messages=cast(Any, self.messages),
                        tools=cast(Any, TOOLS),
                        tool_choice="auto",
                        max_tokens=self.max_tokens,
                        api_key=self.api_key,
                        api_base=self.api_base,
                        stream=True,
                    ),
                )
            except Exception as exc:
                logger.exception("LLM request failed")
                yield Event("error", {"message": str(exc)})
                return

            text = ""
            buffers: dict[int, ToolCallBuffer] = {}

            try:
                async for chunk in stream:
                    if self._cancel_event.is_set():
                        # Stop early; do not persist partial assistant.
                        yield Event("error", {"message": "cancelled"})
                        return

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    if getattr(delta, "content", None):
                        part = delta.content
                        text += part
                        yield Event("text", {"content": part})

                    tool_deltas = getattr(delta, "tool_calls", None)
                    if tool_deltas:
                        for tc in tool_deltas:
                            idx = int(getattr(tc, "index", 0))
                            buf = buffers.get(idx)
                            if buf is None:
                                buf = ToolCallBuffer(index=idx)
                                buffers[idx] = buf

                            if getattr(tc, "id", None):
                                buf.id = tc.id

                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    buf.name = fn.name
                                arg_delta = getattr(fn, "arguments", None)
                                if arg_delta and not buf._args_complete:
                                    buf.arguments += arg_delta
                                    # Detect when we have a complete JSON object;
                                    # subsequent deltas from misbehaving proxies are dropped.
                                    try:
                                        json.loads(buf.arguments)
                                        buf._args_complete = True
                                    except json.JSONDecodeError:
                                        pass  # still accumulating partial JSON
                                elif arg_delta and buf._args_complete:
                                    logger.debug(
                                        "tool[%d] dropping extra argument delta (already complete): %r",
                                        idx,
                                        arg_delta,
                                    )

            except Exception as exc:
                logger.exception("LLM stream failed")
                yield Event("error", {"message": str(exc)})
                return

            # Persist assistant message
            tool_calls: list[dict[str, Any]] = []
            if buffers:
                for idx in sorted(buffers.keys()):
                    buf = buffers[idx]
                    tool_id = buf.id or uuid4().hex
                    tool_calls.append(
                        {
                            "id": tool_id,
                            "type": "function",
                            "function": {"name": buf.name or "", "arguments": buf.arguments or ""},
                        }
                    )

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls

            await self._persist_message(assistant_msg, on_persist)

            if not tool_calls:
                return

            # Execute tools sequentially
            for tc in tool_calls:
                tool_id = tc.get("id") or uuid4().hex
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments")

                parsed = parse_tool_arguments(raw_args)
                if isinstance(parsed, str):
                    # Parse error
                    yield Event("tool_start", {"id": tool_id, "name": name, "args": {}})
                    result = f"error: {parsed}"
                    yield Event("tool_done", {"id": tool_id, "name": name, "result": result})
                    await self._record_tool_result(tool_id=tool_id, result=result, on_persist=on_persist)
                    continue

                args = parsed
                yield Event("tool_start", {"id": tool_id, "name": name, "args": args})

                if self._cancel_event.is_set():
                    result = "error: cancelled"
                    yield Event("tool_done", {"id": tool_id, "name": name, "result": result})
                    await self._record_tool_result(tool_id=tool_id, result=result, on_persist=on_persist)
                    return

                try:
                    if name == "read":
                        result = self.tools.read(**args)
                    elif name == "write":
                        result = self.tools.write(**args)
                    elif name == "edit":
                        result = self.tools.edit(**args)
                    elif name == "bash":
                        command = str(args.get("command", ""))
                        timeout = args.get("timeout")

                        loop = asyncio.get_running_loop()
                        queue: asyncio.Queue[str | None] = asyncio.Queue()

                        def on_output(
                            line: str,
                            _loop: asyncio.AbstractEventLoop = loop,
                            _queue: asyncio.Queue[str | None] = queue,
                        ) -> None:
                            _loop.call_soon_threadsafe(_queue.put_nowait, line)

                        task = asyncio.create_task(
                            _run_bash_to_queue(
                                self.tools,
                                tool_call_id=tool_id,
                                command=command,
                                timeout=timeout,
                                queue=queue,
                                on_output=on_output,
                            )
                        )

                        cancelled = False
                        while True:
                            if self._cancel_event.is_set() and not cancelled:
                                cancelled = True
                                cancel_all_tools()

                            try:
                                item = await asyncio.wait_for(queue.get(), timeout=0.1)
                            except TimeoutError:
                                if task.done():
                                    break
                                continue

                            if item is None:
                                break

                            if not cancelled:
                                yield Event("tool_output", {"id": tool_id, "name": name, "content": item})

                        if cancelled:
                            try:
                                await task
                            except Exception:
                                pass
                            result = "error: cancelled"
                        else:
                            result = await task
                    else:
                        result = f"error: unknown tool: {name}"
                except Exception as exc:
                    result = f"error: {exc}"

                yield Event("tool_done", {"id": tool_id, "name": name, "result": result})
                await self._record_tool_result(tool_id=tool_id, result=result, on_persist=on_persist)

        yield Event("error", {"message": "max_turns reached"})
