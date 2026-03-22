"""Agent loop with provider-specific adapters and one internal message model."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mycode.core.config import Settings, get_settings
from mycode.core.instructions import load_instructions_prompt
from mycode.core.messages import ConversationMessage, build_message, tool_result_block, user_text_message
from mycode.core.providers import get_provider_adapter
from mycode.core.providers.base import ProviderRequest
from mycode.core.skills import load_skills_prompt
from mycode.core.tools import TOOLS, ToolExecutor

logger = logging.getLogger(__name__)

PersistCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Event:
    """Streaming event emitted by the agent."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


async def _run_bash_to_queue(
    tools: ToolExecutor,
    *,
    tool_call_id: str,
    command: str,
    timeout: Any,
    queue: asyncio.Queue[str | None],
    on_output: Callable[[str], None],
) -> str:
    """Run bash in a worker thread and signal the async queue when it ends."""

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
        return "You are mycode, an expert coding assistant."


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
        reasoning_effort: str | None = None,
        settings: Settings | None = None,
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
        self.reasoning_effort = reasoning_effort
        self.settings = settings or get_settings(self.cwd)
        self._cancel_event = asyncio.Event()

        prompt_parts = [_load_system_prompt()]
        instructions_prompt = load_instructions_prompt(self.cwd, self.settings)
        skills_prompt = load_skills_prompt(self.cwd)
        if instructions_prompt:
            prompt_parts.append(instructions_prompt)
        if skills_prompt:
            prompt_parts.append(skills_prompt)
        prompt_parts.append(f"Current working directory: {self.cwd}")
        self.system = "\n\n".join(prompt_parts)

        self.messages: list[ConversationMessage] = list(messages or [])
        self._finalize_pending_tool_results()
        self.tools = ToolExecutor(cwd=self.cwd, session_dir=self.session_dir)

    def _finalize_pending_tool_results(self) -> None:
        """Ensure the last tool-use turn always has matching tool results.

        A previous run may have been cancelled after the assistant emitted tool
        calls but before all tool results were persisted. We recover that gap so
        the next provider turn always sees a closed tool loop.
        """

        last_tool_use_ids: list[str] = []
        last_idx: int | None = None

        for idx in range(len(self.messages) - 1, -1, -1):
            message = self.messages[idx]
            if message.get("role") != "assistant":
                continue

            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue

            last_tool_use_ids = [
                str(block.get("id"))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
            ]
            if last_tool_use_ids:
                last_idx = idx
                break

        if last_idx is None:
            return

        seen: set[str] = set()
        for message in self.messages[last_idx + 1 :]:
            if message.get("role") != "user":
                continue

            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue

            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    seen.add(str(block["tool_use_id"]))

        missing = [tool_use_id for tool_use_id in last_tool_use_ids if tool_use_id not in seen]
        if not missing:
            return

        self.messages.append(
            build_message(
                "user",
                [
                    tool_result_block(
                        tool_use_id=tool_use_id,
                        content="error: tool call was interrupted (no result recorded)",
                        is_error=True,
                    )
                    for tool_use_id in missing
                ],
            )
        )

    def cancel(self) -> None:
        self._cancel_event.set()

    def clear(self) -> None:
        self.messages = []

    async def _run_bash_tool(self, *, tool_id: str, args: dict[str, Any]) -> AsyncIterator[Event]:
        """Stream bash output while the subprocess is still running."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        command = str(args.get("command", ""))
        timeout = args.get("timeout")

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

        try:
            if name == "bash":
                async for event in self._run_bash_tool(tool_id=tool_id, args=args):
                    yield event
                return
            if name == "read":
                result = self.tools.read(**args)
            elif name == "write":
                result = self.tools.write(**args)
            elif name == "edit":
                result = self.tools.edit(**args)
            else:
                result = f"error: unknown tool: {name}"
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

            try:
                # Phase 1: ask the provider for exactly one assistant turn.
                provider_stream = adapter.stream_turn(
                    ProviderRequest(
                        provider=self.provider,
                        model=self.model,
                        session_id=self.session_id,
                        messages=self.messages,
                        system=self.system,
                        tools=TOOLS,
                        max_tokens=self.max_tokens,
                        api_key=self.api_key,
                        api_base=self.api_base,
                        reasoning_effort=self.reasoning_effort,
                    )
                )

                async for provider_event in provider_stream:
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
                return

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

        yield Event("error", {"message": "max_turns reached"})
