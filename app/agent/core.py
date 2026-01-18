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

    def _execute_tool(self, name: str, args: dict) -> str:
        fn = TOOL_MAP.get(name)
        if not fn:
            return f"error: unknown tool {name}"
        return fn(**args)

    async def achat(self, user_input: str, max_iterations: int = 10) -> AsyncIterator[Event]:
        """Async streaming chat."""
        original_cwd = os.getcwd()
        os.chdir(self.cwd)

        try:
            self.messages.append({"role": "user", "content": user_input})

            for _ in range(max_iterations):
                try:
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
                tool_calls_data: dict[str, dict] = {}
                current_tool_id: str | None = None

                try:
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
                                    tool_calls_data[current_tool_id] = {
                                        "id": tc.id,
                                        "name": "",
                                        "arguments": "",
                                    }
                                if current_tool_id and tc.function:
                                    if tc.function.name:
                                        tool_calls_data[current_tool_id]["name"] = tc.function.name
                                    if tc.function.arguments:
                                        tool_calls_data[current_tool_id]["arguments"] += tc.function.arguments
                except Exception as exc:
                    logger.exception("LLM stream failed")
                    yield Event("error", {"message": str(exc)})
                    break

                if not full_content and not tool_calls_data:
                    yield Event(
                        "error",
                        {"message": ("LLM returned empty response. Check model or api_base.")},
                    )
                    break

                assistant_msg = {"role": "assistant", "content": full_content or None}
                if tool_calls_data:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_calls_data.values()
                    ]
                self.messages.append(assistant_msg)

                if not tool_calls_data:
                    break

                tool_results: list[dict] = []
                for tc in tool_calls_data.values():
                    name = tc["name"]
                    args = json.loads(tc["arguments"])

                    yield Event("tool_start", {"name": name, "args": args, "id": tc["id"]})

                    result = ""
                    if name == "bash":
                        cmd = args.get("cmd")
                        if not isinstance(cmd, str) or not cmd.strip():
                            result = "error: missing cmd"
                        else:
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
                                    {"name": name, "content": line, "id": tc["id"]},
                                )
                            result = await task
                    else:
                        result = self._execute_tool(name, args)

                    yield Event(
                        "tool_done",
                        {"name": name, "result": result, "id": tc["id"]},
                    )

                    tool_results.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

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
