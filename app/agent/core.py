from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from any_llm import acompletion

from app.agent.tools import TOOL_MAP, TOOLS, cancel_all_tools

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Agent event for streaming output."""

    type: str
    data: dict = field(default_factory=dict)


class Agent:
    """Minimal coding agent with streaming support."""

    def __init__(self, model: str, cwd: str = ".", api_base: str | None = None):
        self.model = model
        self.cwd = os.path.abspath(cwd)
        self.api_base = api_base
        self.messages: list[dict] = []
        self._init_system()

    def _init_system(self) -> None:
        self.messages = [{"role": "system", "content": f"Concise coding assistant. cwd: {self.cwd}"}]

    def clear(self) -> None:
        self._init_system()

    def cancel(self) -> None:
        """Cancel all running tool processes."""
        cancel_all_tools()

    async def achat(self, user_input: str, max_iterations: int = 10) -> AsyncIterator[Event]:
        """Async streaming chat."""
        original_cwd = os.getcwd()
        os.chdir(self.cwd)

        try:
            # Phase 0: finalize any interrupted tool calls (provider requires tool_result).
            last_tool_idx: int | None = None
            for idx in range(len(self.messages) - 1, -1, -1):
                msg = self.messages[idx]
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    last_tool_idx = idx
                    break
            if last_tool_idx is not None:
                tool_calls = self.messages[last_tool_idx].get("tool_calls") or []
                tool_ids = [tc.get("id") for tc in tool_calls if tc.get("id")]
                if tool_ids:
                    seen: set[str] = set()
                    for msg in self.messages[last_tool_idx + 1 :]:
                        if msg.get("role") == "tool" and msg.get("tool_call_id") in tool_ids:
                            seen.add(msg["tool_call_id"])
                    missing = [tool_id for tool_id in tool_ids if tool_id not in seen]
                    if missing:
                        logger.warning("Finalizing %d pending tool calls", len(missing))
                        for tool_id in missing:
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "content": "error: tool call was interrupted or cancelled",
                                }
                            )

            # Phase 1: append user message.
            self.messages.append({"role": "user", "content": user_input})

            for _ in range(max_iterations):
                try:
                    # Phase 2: request streaming completion.
                    stream = await acompletion(
                        model=self.model,
                        messages=self.messages,
                        max_tokens=64000,
                        tools=TOOLS,
                        api_base=self.api_base,
                        stream=True,
                    )
                except Exception as exc:
                    logger.exception("LLM request failed")
                    yield Event("error", {"message": str(exc)})
                    break

                full_content = ""
                tool_calls: dict[str, dict] = {}
                tool_call_order: list[str] = []
                current_tool_id: str | None = None

                try:
                    # Phase 3: read stream and collect text + tool calls.
                    async for chunk in stream:
                        if not chunk.choices:
                            # Usage chunk at end of stream has empty choices - this is normal
                            continue
                        delta = chunk.choices[0].delta

                        if delta.content:
                            full_content += delta.content
                            yield Event("text", {"content": delta.content})

                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                if tc.id:
                                    current_tool_id = tc.id
                                    if current_tool_id not in tool_calls:
                                        tool_calls[current_tool_id] = {
                                            "id": tc.id,
                                            "name": "",
                                            "arguments": "",
                                        }
                                        tool_call_order.append(current_tool_id)
                                if current_tool_id and tc.function:
                                    if tc.function.name:
                                        tool_calls[current_tool_id]["name"] = tc.function.name
                                    if tc.function.arguments:
                                        tool_calls[current_tool_id]["arguments"] += tc.function.arguments
                except Exception as exc:
                    logger.exception("LLM stream failed")
                    yield Event("error", {"message": str(exc)})
                    break

                if not full_content and not tool_calls:
                    yield Event(
                        "error",
                        {"message": ("LLM returned empty response. Check model or api_base.")},
                    )
                    break

                # Phase 4: persist assistant message (and tool calls if any).
                assistant_msg = {"role": "assistant", "content": full_content or None}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in (tool_calls[tool_id] for tool_id in tool_call_order)
                    ]
                self.messages.append(assistant_msg)

                if not tool_calls:
                    break

                # Phase 5: execute tool calls and append tool results.
                tool_results: list[dict] = []
                for tool_id in tool_call_order:
                    tc = tool_calls[tool_id]
                    name = tc["name"]
                    args: dict = {}
                    parse_error: Exception | None = None
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                        if not isinstance(args, dict):
                            raise ValueError("tool arguments must be an object")
                    except Exception as exc:
                        parse_error = exc

                    yield Event("tool_start", {"name": name, "args": args, "id": tool_id})

                    if parse_error:
                        result = f"error: invalid tool arguments: {parse_error}"
                        yield Event("tool_done", {"name": name, "result": result, "id": tool_id})
                        tool_results.append({"role": "tool", "tool_call_id": tool_id, "content": result})
                        continue

                    result = ""
                    try:
                        if name == "bash":
                            cmd = args.get("cmd")
                            if not isinstance(cmd, str) or not cmd.strip():
                                result = "error: missing cmd"
                            else:
                                # Stream bash output line-by-line.
                                queue: asyncio.Queue[str | None] = asyncio.Queue()
                                loop = asyncio.get_running_loop()

                                def on_output(line: str, *, _loop=loop, _queue=queue) -> None:
                                    _loop.call_soon_threadsafe(_queue.put_nowait, line)

                                def run_bash(*, _cmd=cmd, _loop=loop, _queue=queue) -> str:
                                    try:
                                        return TOOL_MAP["bash"](_cmd, on_output)
                                    finally:
                                        _loop.call_soon_threadsafe(_queue.put_nowait, None)

                                task = asyncio.create_task(asyncio.to_thread(run_bash))
                                while True:
                                    line = await queue.get()
                                    if line is None:
                                        break
                                    yield Event(
                                        "tool_output",
                                        {"name": name, "content": line, "id": tool_id},
                                    )
                                result = await task
                        else:
                            fn = TOOL_MAP.get(name)
                            result = f"error: unknown tool {name}" if not fn else fn(**args)
                    except Exception as exc:
                        result = f"error: {exc}"

                    yield Event(
                        "tool_done",
                        {"name": name, "result": result, "id": tool_id},
                    )

                    tool_results.append({"role": "tool", "tool_call_id": tool_id, "content": result})

                self.messages.extend(tool_results)

        except Exception as exc:
            yield Event("error", {"message": str(exc)})
        finally:
            os.chdir(original_cwd)


def get_model_config() -> tuple[str | None, str | None]:
    """Auto-detect model from environment."""
    model = os.environ.get("MODEL")
    api_base = os.environ.get("BASE_URL")

    if not model:
        if os.environ.get("ANTHROPIC_API_KEY"):
            model = "anthropic:claude-opus-4-5"
        elif os.environ.get("OPENAI_API_KEY"):
            model = "openai:gpt-5.2"
        elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            model = "gemini:gemini-3-flash-preview"

    return model, api_base
