"""Public SDK surface tests for core runtime behavior."""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel, Field

from mycode import (
    Agent,
    SessionStore,
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolSpec,
    bash_tool,
    edit_tool,
    read_tool,
    text_block,
    tool,
    write_tool,
)
from mycode.providers.base import ProviderStreamEvent


class _CaptureAdapter:
    def __init__(self, text: str = "ok") -> None:
        self.requests = []
        self.message_snapshots = []
        self.text = text

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        self.message_snapshots.append(deepcopy(request.messages))
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": self.text}]}},
        )


class _StreamingAdapter:
    def __init__(self, text: str = "ok") -> None:
        self.text = text

    async def stream_turn(self, _request) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent("thinking_delta", {"text": "plan "})
        yield ProviderStreamEvent("text_delta", {"text": self.text})
        yield ProviderStreamEvent(
            "message_done",
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "plan "},
                        {"type": "text", "text": self.text},
                    ],
                }
            },
        )


class _ToolLoopAdapter:
    def __init__(self, tool_name: str = "read_back") -> None:
        self.requests = []
        self.tool_name = tool_name

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderStreamEvent(
                "message_done",
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": self.tool_name,
                                "input": {"path": "note.txt"},
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


@tool
def read_back(context: ToolContext, path: str) -> str:
    """Read a file through the built-in read tool."""

    return context.read(path).output


@tool(streams_output=True)
async def read_back_async(context: ToolContext, path: str) -> str:
    """Read a file through the async built-in read tool."""

    output = (await context.aread(path)).output
    if context.emit:
        context.emit(output)
    return output


def _new_agent(tmp_path: Path, **overrides) -> Agent:
    overrides.setdefault("model", "gpt-5.5")
    overrides.setdefault("cwd", str(tmp_path))
    overrides.setdefault("session_dir", tmp_path)
    overrides.setdefault("session_id", "session")
    return Agent(**overrides)


def test_agent_tools_are_explicitly_opt_in(tmp_path: Path) -> None:
    default_agent = _new_agent(tmp_path)
    explicit_agent = _new_agent(tmp_path, tools=[read_tool, write_tool, edit_tool, bash_tool])

    assert default_agent.tools.definitions == []
    assert {tool_def["name"] for tool_def in explicit_agent.tools.definitions} == {
        "bash",
        "edit",
        "read",
        "write",
    }


def test_agent_requires_explicit_provider_for_unknown_models(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not infer provider"):
        _new_agent(tmp_path, model="not-a-real-model")


def test_agent_run_collects_stream_events(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path)

    with patch("mycode.agent.get_provider_adapter", return_value=_StreamingAdapter("hello")):
        result = agent.run("hi")

    assert result.text == "hello"
    assert result.error is None
    assert [event.type for event in result.events] == ["reasoning", "reasoning_done", "text"]
    assert result.events[0].data == {"delta": "plan "}
    assert isinstance(result.events[1].data.get("duration_ms"), int)
    assert result.events[2].data == {"delta": "hello"}


def test_agent_run_rejects_non_user_messages(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path)

    with patch("mycode.agent.get_provider_adapter", return_value=_CaptureAdapter("ok")):
        result = agent.run({"role": "assistant", "content": [text_block("bad")]})

    assert result.text == ""
    assert result.error == "user input must be a user message"
    assert [event.type for event in result.events] == ["error"]


@pytest.mark.asyncio
class TestAsyncAgentBehavior:
    async def test_achat_persists_messages_to_the_session_store(self, tmp_path: Path) -> None:
        agent = _new_agent(tmp_path, session_id="s1")

        with patch("mycode.agent.get_provider_adapter", return_value=_CaptureAdapter("first reply")):
            _ = [event async for event in agent.achat("first question")]

        loaded = await SessionStore(data_dir=tmp_path).load_session("s1")

        assert loaded is not None
        assert [message["role"] for message in loaded["messages"]] == ["user", "assistant"]

    async def test_achat_resumes_existing_session_history(self, tmp_path: Path) -> None:
        first = _new_agent(tmp_path, session_id="s2")
        with patch("mycode.agent.get_provider_adapter", return_value=_CaptureAdapter("first reply")):
            _ = [event async for event in first.achat("first question")]

        second = _new_agent(tmp_path, session_id="s2")
        adapter = _CaptureAdapter("second reply")

        assert [message["role"] for message in second.messages] == ["user", "assistant"]

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            _ = [event async for event in second.achat("second question")]

        assert [message["content"][0]["text"] for message in adapter.message_snapshots[0]] == [
            "first question",
            "first reply",
            "second question",
        ]

    async def test_rejects_explicit_messages_for_existing_sessions(self, tmp_path: Path) -> None:
        agent = _new_agent(tmp_path, session_id="s3")
        with patch("mycode.agent.get_provider_adapter", return_value=_CaptureAdapter("ok")):
            _ = [event async for event in agent.achat("hi")]

        with pytest.raises(ValueError, match="already exists"):
            _new_agent(tmp_path, session_id="s3", messages=[])

        with pytest.raises(ValueError, match="already exists"):
            _new_agent(
                tmp_path,
                session_id="s3",
                messages=[{"role": "user", "content": [text_block("fake history")]}],
            )

    async def test_custom_tools_can_reuse_builtin_tools(self, tmp_path: Path) -> None:
        note_path = tmp_path / "note.txt"
        note_path.write_text("hello from sdk\n", encoding="utf-8")

        agent = _new_agent(tmp_path, tools=[read_tool, read_back])
        adapter = _ToolLoopAdapter()

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            events = [event async for event in agent.achat("Read note.txt and repeat it.")]

        assert [event.type for event in events] == ["tool_start", "tool_done"]
        assert events[1].data["output"] == "hello from sdk"

    async def test_async_custom_tools_can_reuse_and_stream_builtin_output(self, tmp_path: Path) -> None:
        note_path = tmp_path / "note.txt"
        note_path.write_text("hello from async sdk\n", encoding="utf-8")

        agent = _new_agent(tmp_path, tools=[read_tool, read_back_async])
        adapter = _ToolLoopAdapter("read_back_async")

        with patch("mycode.agent.get_provider_adapter", return_value=adapter):
            events = [event async for event in agent.achat("Read note.txt and repeat it.")]

        assert [event.type for event in events] == ["tool_start", "tool_output", "tool_done"]
        assert events[1].data["output"] == "hello from async sdk"
        assert events[2].data["output"] == "hello from async sdk"


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_async_tool_can_use_loop_bound_resource(self, tmp_path: Path) -> None:
        loop = asyncio.get_running_loop()
        response = loop.create_future()
        loop.call_later(0.01, response.set_result, "ready")

        @tool
        async def wait_for_response() -> str:
            """Wait for an event-loop-bound response."""

            return await response

        executor = ToolExecutor([wait_for_response])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")

        result = await executor.aexecute("wait_for_response", {}, ctx)

        assert result.output == "ready"

    def test_sync_executor_runs_async_tool(self, tmp_path: Path) -> None:
        @tool
        async def greet() -> str:
            """Return a greeting."""

            await asyncio.sleep(0)
            return "hello"

        executor = ToolExecutor([greet])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")

        result = executor.execute("greet", {}, ctx)

        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_executor_awaits_wrapped_async_callable_runner(self, tmp_path: Path) -> None:
        class AsyncRunner:
            async def __call__(self, _ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
                await asyncio.sleep(0)
                return ToolExecutionResult(output=str(args["value"]))

        spec = ToolSpec(
            name="echo",
            description="Echo a value.",
            input_schema={"type": "object"},
            runner=functools.partial(AsyncRunner()),
        )
        executor = ToolExecutor([spec])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")

        result = await executor.aexecute("echo", {"value": "async callable"}, ctx)

        assert result.output == "async callable"

    @pytest.mark.asyncio
    async def test_sync_tool_does_not_block_event_loop(self, tmp_path: Path) -> None:
        started = threading.Event()
        release = threading.Event()

        @tool
        def wait_for_release() -> str:
            """Wait until the test releases the tool."""

            started.set()
            release.wait(timeout=1)
            return "released"

        executor = ToolExecutor([wait_for_release])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")
        task = asyncio.create_task(executor.aexecute("wait_for_release", {}, ctx))

        assert await asyncio.to_thread(started.wait, 1)
        assert not task.done()
        release.set()
        result = await task

        assert result.output == "released"


class TestToolDecorator:
    def test_infers_json_schema_from_signature(self) -> None:
        @tool
        def lookup(key: str, limit: int = 10) -> str:
            """Find entries matching ``key``."""

            return f"{key}:{limit}"

        assert lookup.name == "lookup"
        assert lookup.description.startswith("Find entries")
        assert lookup.input_schema["properties"]["key"] == {"type": "string"}
        assert lookup.input_schema["properties"]["limit"] == {"type": "integer", "default": 10}
        assert lookup.input_schema["required"] == ["key"]
        assert lookup.streams_output is False

    def test_preserves_parameters_named_like_schema_metadata(self) -> None:
        @tool
        def render(format: str, title: str, additionalProperties: str) -> str:
            """Render a document."""

            return f"{format}:{title}:{additionalProperties}"

        assert render.input_schema["properties"]["format"] == {"type": "string"}
        assert render.input_schema["properties"]["title"] == {"type": "string"}
        assert render.input_schema["properties"]["additionalProperties"] == {"type": "string"}
        assert render.input_schema["required"] == ["format", "title", "additionalProperties"]

    def test_converts_path_arguments_before_calling_runner(self, tmp_path: Path) -> None:
        captured: dict[str, object] = {}

        @tool
        def show(target: Path) -> str:
            """Echo the target path type and value."""

            captured["value"] = target
            return str(target)

        executor = ToolExecutor([show])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")
        result = executor.execute("show", {"target": "/etc/hosts"}, ctx)

        assert show.input_schema["properties"]["target"] == {"type": "string"}
        assert isinstance(captured["value"], Path)
        assert captured["value"] == Path("/etc/hosts")
        assert result.output == "/etc/hosts"

    def test_uses_default_when_non_nullable_default_parameter_receives_null(self, tmp_path: Path) -> None:
        @tool
        def lookup(key: str, limit: int = 10) -> str:
            """Find entries."""

            return f"{key}:{limit}"

        executor = ToolExecutor([lookup])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")
        result = executor.execute("lookup", {"key": "a", "limit": None}, ctx)

        assert result.output == "a:10"
        assert result.is_error is False

    def test_decorator_metadata_overrides_docstring(self) -> None:
        @tool(
            name="find",
            description="Decorator description.",
            parameters={"key": "Decorator key.", "limit": "Decorator limit."},
        )
        def lookup(key: str, limit: int = 10) -> str:
            """Docstring description.

            Args:
                key: Docstring key.
                limit: Docstring limit.
            """

            return f"{key}:{limit}"

        assert lookup.name == "find"
        assert lookup.description == "Decorator description."
        assert lookup.input_schema["properties"]["key"]["description"] == "Decorator key."
        assert lookup.input_schema["properties"]["limit"]["description"] == "Decorator limit."

    def test_rejects_unknown_decorator_parameter_descriptions(self) -> None:
        with pytest.raises(ValueError, match="unknown parameter descriptions"):

            @tool(parameters={"missing": "Typo."})
            def lookup(key: str) -> str:
                """Find entries."""

                return key

    def test_rejects_dict_parameters(self) -> None:
        with pytest.raises(TypeError, match="dict/map input"):

            @tool
            def search(filters: dict[str, str]) -> str:
                """Search entries."""

                return str(filters)

    def test_supports_nested_pydantic_models(self, tmp_path: Path) -> None:
        class EditEntry(BaseModel):
            old_text: str = Field(alias="oldText", description="Exact text to find.")
            new_text: str = Field(alias="newText", description="Replacement text.")

        captured: dict[str, object] = {}

        @tool(parameters={"path": "File path.", "edits": "Replacement entries."})
        def replace(path: str, edits: list[EditEntry]) -> str:
            """Replace text snippets."""

            captured["edit"] = edits[0]
            return path

        executor = ToolExecutor([replace])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")
        result = executor.execute("replace", {"path": "x.txt", "edits": [{"oldText": "a", "newText": "b"}]}, ctx)

        assert result.output == "x.txt"
        assert isinstance(captured["edit"], EditEntry)
        assert captured["edit"].old_text == "a"
        assert replace.input_schema["properties"]["path"] == {"type": "string", "description": "File path."}
        assert replace.input_schema["properties"]["edits"] == {
            "description": "Replacement entries.",
            "items": {"$ref": "#/$defs/EditEntry"},
            "type": "array",
        }
        assert replace.input_schema["$defs"]["EditEntry"]["properties"]["oldText"] == {
            "description": "Exact text to find.",
            "type": "string",
        }
