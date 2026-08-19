"""Tests for SDK tool execution hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from time import sleep
from typing import cast
from unittest.mock import patch

import pytest

from mycode import Agent, Event, Hooks, ToolExecutionResult, ToolHookContext, tool
from mycode.providers.base import ProviderStreamEvent
from mycode.tools import ToolContext, ToolSpec


def _chat_events(events: list[Event]) -> list[Event]:
    """Drop the per-request usage events; usage flows have dedicated tests."""

    return [event for event in events if event.type != "usage"]


class _ToolUseAdapter:
    def __init__(self, tool_name: str, tool_input: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.requests = 0

    async def stream_turn(self, _request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests += 1
        if self.requests == 1:
            yield ProviderStreamEvent(
                "message_done",
                {
                    "message": {
                        "role": "assistant",
                        "meta": {"stop_reason": "tool_use"},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": self.tool_name,
                                "input": self.tool_input,
                            }
                        ],
                    }
                },
            )
            return

        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
        )


def _agent(tmp_path: Path, *, tool: ToolSpec, hooks: Hooks) -> Agent:
    return Agent(
        model="gpt-5.5",
        provider="openai",
        session_dir=tmp_path,
        session_id="session",
        tools=[tool],
        hooks=hooks,
    )


def _tool(runner) -> ToolSpec:
    return ToolSpec(
        name="ping",
        description="Test tool.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        runner=runner,
    )


@pytest.mark.asyncio
async def test_agent_passes_the_same_deps_to_parameterized_tools_and_hooks() -> None:
    deps = object()
    seen: list[object] = []
    hooks = Hooks()

    @tool
    def inspect(ctx: ToolContext[object]) -> str:
        """Inspect the application dependencies."""

        seen.append(ctx.deps)
        return "ok"

    @hooks.before_tool
    def observe(ctx: ToolHookContext[object]) -> None:
        seen.append(ctx.deps)

    agent = Agent(
        model="gpt-5.5",
        provider="openai",
        tools=[inspect],
        hooks=hooks,
        deps=deps,
    )
    adapter = _ToolUseAdapter("inspect", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("inspect")])

    assert events[-1].data["output"] == "ok"
    assert len(seen) == 2
    assert all(value is deps for value in seen)


@pytest.mark.asyncio
async def test_before_tool_blocks_execution_and_after_hooks_see_result(tmp_path: Path) -> None:
    calls: list[str] = []
    seen: list[str] = []
    hooks = Hooks()

    def runner(_ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        calls.append("tool")
        return ToolExecutionResult(output="tool ran")

    @hooks.before_tool
    def block(ctx: ToolHookContext[None]) -> ToolExecutionResult:
        assert ctx.tool_name == "ping"
        assert ctx.tool_call_id == "call-1"
        assert ctx.deps is None
        return ToolExecutionResult(output="blocked", is_error=True)

    @hooks.after_tool
    async def audit(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> None:
        seen.append(result.output)

    agent = _agent(tmp_path, tool=_tool(runner), hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert calls == []
    assert seen == ["blocked"]
    assert [event.type for event in events] == ["tool_start", "tool_done"]
    assert events[1].data == {"tool_use_id": "call-1", "output": "blocked", "is_error": True}


@pytest.mark.asyncio
async def test_after_tool_hooks_replace_results_in_order(tmp_path: Path) -> None:
    seen: list[str] = []
    hooks = Hooks()

    def runner(_ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        return ToolExecutionResult(output="tool")

    @hooks.after_tool
    def first(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> ToolExecutionResult:
        seen.append(result.output)
        return ToolExecutionResult(output=f"{result.output}+first")

    @hooks.after_tool
    async def second(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> ToolExecutionResult:
        seen.append(result.output)
        return ToolExecutionResult(output=f"{result.output}+second")

    agent = _agent(tmp_path, tool=_tool(runner), hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert seen == ["tool", "tool+first"]
    assert events[1].data == {"tool_use_id": "call-1", "output": "tool+first+second", "is_error": False}


@pytest.mark.asyncio
async def test_tool_hook_context_input_is_readonly(tmp_path: Path) -> None:
    hooks = Hooks()

    def runner(_ctx: ToolContext[None], args: dict[str, object]) -> ToolExecutionResult:
        assert args == {"text": "hello", "items": [{"value": "original"}]}
        return ToolExecutionResult(output="ok")

    @hooks.before_tool
    def inspect_input(ctx: ToolHookContext[None]) -> None:
        tool_input = cast(dict[str, object], ctx.tool_input)
        with pytest.raises(TypeError):
            tool_input["text"] = "changed"

        items = cast(tuple[dict[str, object], ...], ctx.tool_input["items"])
        assert isinstance(items, tuple)
        with pytest.raises(TypeError):
            items[0]["value"] = "changed"

    agent = _agent(tmp_path, tool=_tool(runner), hooks=hooks)
    adapter = _ToolUseAdapter("ping", {"text": "hello", "items": [{"value": "original"}]})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert events[1].data == {"tool_use_id": "call-1", "output": "ok", "is_error": False}


@pytest.mark.asyncio
async def test_hook_errors_become_tool_errors(tmp_path: Path) -> None:
    calls: list[str] = []
    after_calls: list[str] = []
    hooks = Hooks()

    def runner(_ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        calls.append("tool")
        return ToolExecutionResult(output="tool ran")

    @hooks.before_tool
    def broken(_ctx: ToolHookContext[None]) -> None:
        raise RuntimeError("boom")

    @hooks.after_tool
    def audit(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> None:
        after_calls.append(result.output)

    agent = _agent(tmp_path, tool=_tool(runner), hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert calls == []
    assert after_calls == []
    assert events[1].data == {
        "tool_use_id": "call-1",
        "output": "error: tool hook failed: boom",
        "is_error": True,
    }


@pytest.mark.asyncio
async def test_after_tool_hook_errors_keep_tool_result(tmp_path: Path) -> None:
    hooks = Hooks()

    def runner(_ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        return ToolExecutionResult(output="tool ran")

    @hooks.after_tool
    def broken(_ctx: ToolHookContext[None], _result: ToolExecutionResult) -> None:
        raise RuntimeError("boom")

    agent = _agent(tmp_path, tool=_tool(runner), hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert events[1].data == {"tool_use_id": "call-1", "output": "tool ran", "is_error": False}


@pytest.mark.asyncio
async def test_cancellation_after_before_hooks_cannot_be_replaced(tmp_path: Path) -> None:
    calls: list[str] = []
    after_calls: list[str] = []
    hooks = Hooks()

    def runner(_ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        calls.append("tool")
        return ToolExecutionResult(output="tool ran")

    @hooks.before_tool
    def cancel(_ctx: ToolHookContext[None]) -> None:
        agent.cancel()

    @hooks.after_tool
    def replace(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> ToolExecutionResult:
        after_calls.append(result.output)
        return ToolExecutionResult(output="replaced")

    agent = _agent(tmp_path, tool=_tool(runner), hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert calls == []
    assert after_calls == []
    assert events[1].data == {
        "tool_use_id": "call-1",
        "output": "error: cancelled",
        "is_error": True,
    }


@pytest.mark.asyncio
async def test_before_tool_blocks_streaming_tool_without_live_output(tmp_path: Path) -> None:
    calls: list[str] = []
    hooks = Hooks()

    def runner(ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        calls.append("tool")
        if ctx.emit:
            ctx.emit("live")
        return ToolExecutionResult(output="streamed")

    tool = ToolSpec(
        name="ping",
        description="Streaming test tool.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        runner=runner,
        streams_output=True,
    )

    @hooks.before_tool
    def block(_ctx: ToolHookContext[None]) -> ToolExecutionResult:
        return ToolExecutionResult(output="blocked", is_error=True)

    agent = _agent(tmp_path, tool=tool, hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert calls == []
    assert [event.type for event in events] == ["tool_start", "tool_done"]
    assert events[1].data == {"tool_use_id": "call-1", "output": "blocked", "is_error": True}


@pytest.mark.asyncio
async def test_after_tool_can_replace_streaming_tool_result(tmp_path: Path) -> None:
    hooks = Hooks()

    def runner(ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        if ctx.emit:
            ctx.emit("live")
        return ToolExecutionResult(output="streamed")

    tool = ToolSpec(
        name="ping",
        description="Streaming test tool.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        runner=runner,
        streams_output=True,
    )

    @hooks.after_tool
    def replace(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> ToolExecutionResult:
        assert result.output == "streamed"
        return ToolExecutionResult(output="replaced")

    agent = _agent(tmp_path, tool=tool, hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = _chat_events([event async for event in agent.achat("run ping")])

    assert [event.type for event in events] == ["tool_start", "tool_output", "tool_done"]
    assert events[1].data == {"tool_use_id": "call-1", "output": "live"}
    assert events[2].data == {"tool_use_id": "call-1", "output": "replaced", "is_error": False}


@pytest.mark.asyncio
async def test_streaming_cancellation_cannot_be_replaced(tmp_path: Path) -> None:
    after_calls: list[str] = []
    hooks = Hooks()

    def runner(ctx: ToolContext[None], _args: dict[str, object]) -> ToolExecutionResult:
        if ctx.emit:
            ctx.emit("live")
        sleep(0.05)
        return ToolExecutionResult(output="streamed")

    tool = ToolSpec(
        name="ping",
        description="Streaming test tool.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        runner=runner,
        streams_output=True,
    )

    @hooks.after_tool
    def replace(_ctx: ToolHookContext[None], result: ToolExecutionResult) -> ToolExecutionResult:
        after_calls.append(result.output)
        return ToolExecutionResult(output="replaced")

    agent = _agent(tmp_path, tool=tool, hooks=hooks)
    adapter = _ToolUseAdapter("ping", {})

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        stream = agent.achat("run ping")
        assert (await anext(stream)).type == "usage"
        first = await anext(stream)
        second = await anext(stream)
        agent.cancel()
        rest = [event async for event in stream]

    assert first.type == "tool_start"
    assert second.data == {"tool_use_id": "call-1", "output": "live"}
    assert after_calls == []
    assert [event.type for event in rest] == ["tool_done"]
    assert rest[0].data == {"tool_use_id": "call-1", "output": "error: cancelled", "is_error": True}
