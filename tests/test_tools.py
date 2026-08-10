"""Tests for the tool runtime: executor dispatch and the @tool decorator."""

from __future__ import annotations

import asyncio
import functools
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from mycode import (
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolSpec,
    tool,
)


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

    def test_invalid_input_does_not_call_user_function(self, tmp_path: Path) -> None:
        calls: list[str] = []

        @tool
        def lookup(key: str) -> str:
            """Find one entry."""

            calls.append(key)
            return key

        executor = ToolExecutor([lookup])
        ctx = ToolContext(executor=executor, cwd=".", tool_output_dir=tmp_path / "_p")
        result = executor.execute("lookup", {}, ctx)

        assert result.is_error is True
        assert result.output.startswith("error: invalid tool input:")
        assert calls == []

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
