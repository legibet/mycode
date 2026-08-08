"""Basic tests for tool execution and truncation."""

from __future__ import annotations

import asyncio
import base64
import functools
import tempfile
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
    bash_tool,
    edit_tool,
    read_tool,
    tool,
    write_tool,
)
from mycode.tools import READ_MAX_LINE_CHARS

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1X8AAAAASUVORK5CYII="
)
_TOOLS = (read_tool, write_tool, edit_tool, bash_tool)


def _ctx(cwd: str, *, tool_output_dir: Path | None = None, supports_image_input: bool = False) -> ToolContext:
    """Build a ToolContext with the four built-ins registered."""

    executor = ToolExecutor(_TOOLS)
    return ToolContext(
        executor=executor,
        cwd=cwd,
        tool_output_dir=tool_output_dir if tool_output_dir is not None else Path(cwd),
        supports_image_input=supports_image_input,
    )


class TestRead:
    def test_read_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _ctx(tmpdir)
            (Path(tmpdir) / "test.txt").write_text("Hello, World!")

            result = ctx.read("test.txt")
            assert result.output == "Hello, World!"

    def test_read_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir).read("nonexistent.txt")

            assert result.is_error is True
            assert "error" in result.output.lower()
            assert "not found" in result.output.lower()

    def test_read_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "subdir").mkdir()

            result = _ctx(tmpdir).read("subdir")
            assert result.is_error is True
            assert "error" in result.output.lower()
            assert "not a file" in result.output.lower()

    def test_read_with_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("line 1\nline 2\nline 3\nline 4")

            result = _ctx(tmpdir).read("test.txt", offset=2, limit=2)
            assert "line 2" in result.output
            assert "line 3" in result.output
            assert "line 1" not in result.output
            assert "line 4" not in result.output

    def test_read_with_limit_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("line 1\nline 2\nline 3\nline 4")

            result = _ctx(tmpdir).read("test.txt", limit=2)
            assert "line 1" in result.output
            assert "line 2" in result.output
            assert "line 3" not in result.output

    def test_read_offset_beyond_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("line 1\nline 2")

            result = _ctx(tmpdir).read("test.txt", offset=10)
            assert result.is_error is True
            assert "error" in result.output.lower()
            assert "beyond" in result.output.lower()

    def test_read_binary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "binary.bin").write_bytes(b"\x80\x81\x82\x83")

            result = _ctx(tmpdir).read("binary.bin")
            assert result.is_error is True
            assert "error" in result.output.lower()
            assert "utf-8" in result.output.lower()

    def test_read_truncated_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lines = [f"line {i}" for i in range(3000)]
            (Path(tmpdir) / "large.txt").write_text("\n".join(lines))

            result = _ctx(tmpdir).read("large.txt")
            assert "[Showing lines 1-2000. Use offset=2001 to continue.]" in result.output

    def test_read_shortens_long_line_and_adds_slice_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "long.txt").write_text("short\n" + ("x" * (READ_MAX_LINE_CHARS + 50)))

            result = _ctx(tmpdir).read("long.txt")

            assert "... [line truncated]" in result.output
            assert f"shortened to {READ_MAX_LINE_CHARS} chars" in result.output
            assert "sed -n '2p'" in result.output
            assert "head -c 2000" in result.output

    def test_read_image_returns_structured_content_when_model_supports_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "tiny.png"
            image_path.write_bytes(_PNG_1X1)

            result = _ctx(tmpdir, supports_image_input=True).read("tiny.png")

            assert result.is_error is False
            assert result.output == "Read image file [image/png]"
            assert result.content == [
                {"type": "text", "text": "Read image file [image/png]"},
                {
                    "type": "image",
                    "data": base64.b64encode(image_path.read_bytes()).decode("utf-8"),
                    "mime_type": "image/png",
                    "name": "tiny.png",
                },
            ]

    def test_read_image_errors_when_model_does_not_support_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "tiny.png").write_bytes(_PNG_1X1)

            result = _ctx(tmpdir, supports_image_input=False).read("tiny.png")

            assert result.is_error is True
            assert "not supported by the current model" in result.output


class TestWrite:
    def test_write_creates_parent_directories_and_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _ctx(tmpdir)
            result = ctx.write("subdir/nested/file.txt", "First")
            written = Path(tmpdir) / "subdir" / "nested" / "file.txt"
            assert result.output == "Wrote subdir/nested/file.txt"
            assert written.read_text() == "First"

            overwritten = ctx.write("subdir/nested/file.txt", "Second")
            assert overwritten.is_error is False
            assert written.read_text() == "Second"


class TestEdit:
    def test_edit_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "World", "newText": "Universe"}])
            assert result.is_error is False
            assert result.metadata is not None
            assert isinstance(result.metadata["patch"], str)
            assert test_file.read_text() == "Hello, Universe!"

    def test_edit_multiple_occurrences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("apple apple apple")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "apple", "newText": "orange"}])
            assert result.is_error is True
            assert "occurs" in result.output.lower()
            assert test_file.read_text() == "apple apple apple"

    def test_edit_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir).edit("nonexistent.txt", [{"oldText": "x", "newText": "y"}])
            assert result.is_error is True
            assert "not found" in result.output.lower()

    def test_edit_rejects_empty_old_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "", "newText": "Replacement"}])
            assert result.is_error is True
            assert "must not be empty" in result.output.lower()
            assert test_file.read_text() == "Hello, World!"

    def test_edit_rejects_no_op(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "World", "newText": "World"}])
            assert result.is_error is True
            assert "identical" in result.output.lower()
            assert test_file.read_text() == "Hello, World!"

    def test_edit_not_found_includes_closest_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("alpha\nbeta gamma\ndelta")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "beta gamam", "newText": "replacement"}])
            assert result.is_error is True
            assert "closest line" in result.output.lower()
            assert "beta gamma" in result.output

    def test_edit_fuzzy_matches_trailing_whitespace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("def f():\n    return 1    \n")

            result = _ctx(tmpdir).edit(
                "test.py",
                [
                    {
                        "oldText": "def f():\n    return 1\n",
                        "newText": "def f():\n    return 2\n",
                    }
                ],
            )
            assert result.is_error is False
            assert test_file.read_text() == "def f():\n    return 2\n"

    def test_edit_fuzzy_matches_crlf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_bytes(b"line1\r\nline2\r\n")

            result = _ctx(tmpdir).edit(
                "test.txt",
                [{"oldText": "line1\nline2\n", "newText": "line1\nlineX\n"}],
            )
            assert result.is_error is False
            assert test_file.read_bytes() == b"line1\r\nlineX\r\n"

    def test_edit_fuzzy_requires_unique_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_bytes(b"x  \r\nx\t\r\n")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "x\n", "newText": "y\n"}])
            assert result.is_error is True
            assert "occurs" in result.output.lower()
            assert "normalization" in result.output.lower()

    # ---- multi-edit tests ----

    def test_multi_edit_disjoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("aaa\nbbb\nccc\nddd\n")

            result = _ctx(tmpdir).edit(
                "test.txt",
                [
                    {"oldText": "aaa", "newText": "AAA"},
                    {"oldText": "ccc", "newText": "CCC"},
                ],
            )
            assert result.is_error is False
            assert test_file.read_text() == "AAA\nbbb\nCCC\nddd\n"

    def test_multi_edit_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("aaa bbb ccc")

            result = _ctx(tmpdir).edit(
                "test.txt",
                [
                    {"oldText": "aaa bbb", "newText": "XXX"},
                    {"oldText": "bbb ccc", "newText": "YYY"},
                ],
            )
            assert result.is_error is True
            assert "overlap" in result.output.lower()
            assert test_file.read_text() == "aaa bbb ccc"

    def test_multi_edit_empty_list_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("content")

            result = _ctx(tmpdir).edit("test.txt", [])
            assert result.is_error is True
            assert "empty" in result.output.lower()

    def test_multi_edit_with_line_expansion(self):
        """Multi-edit where one edit adds lines — later edit metadata should reflect shifted lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("a\nb\nc\n")

            result = _ctx(tmpdir).edit(
                "test.txt",
                [
                    {"oldText": "a", "newText": "a1\na2"},
                    {"oldText": "c", "newText": "C"},
                ],
            )
            assert result.is_error is False
            assert test_file.read_text() == "a1\na2\nb\nC\n"

    def test_multi_edit_patch_uses_file_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")

            result = _ctx(tmpdir).edit(
                "test.txt",
                [
                    {"oldText": "line 5", "newText": "LINE 5"},
                    {"oldText": "line 2", "newText": "LINE 2"},
                ],
            )

            assert result.is_error is False
            assert result.metadata is not None
            patch = result.metadata["patch"]
            assert isinstance(patch, str)
            assert patch.index("-line 2") < patch.index("-line 5")
            assert test_file.read_text() == "line 1\nLINE 2\nline 3\nline 4\nLINE 5\n"

    def test_edit_added_removed_line_stats(self):
        cases = [
            ("foo\n", "foo", "bar", 1, 1),
            ("foo\n", "foo", "foo\nbar\nbaz", 2, 0),
            ("a\nb\nc\n", "a\nb\nc", "a\nc", 0, 1),
            ("x\nold1\nold2\ny\n", "old1\nold2", "new1\nnew2\nnew3", 3, 2),
            ("A\nB\n", "A\nB", "B\nA", 1, 1),
        ]
        for initial, old_text, new_text, expected_added, expected_removed in cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                test_file = Path(tmpdir) / "f.txt"
                test_file.write_text(initial)
                result = _ctx(tmpdir).edit(
                    "f.txt",
                    [{"oldText": old_text, "newText": new_text}],
                )
                assert result.is_error is False
                assert result.metadata is not None
                assert result.metadata["added_lines"] == expected_added
                assert result.metadata["removed_lines"] == expected_removed


def test_read_accepts_absolute_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "abs_test.txt"
        test_file.write_text("Absolute path content")

        result = _ctx("/tmp").read(str(test_file))
        assert "Absolute path content" in result.output


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
