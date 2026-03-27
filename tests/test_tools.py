"""Basic tests for tool execution and truncation."""

import json
import tempfile
from pathlib import Path

import pytest

from mycode.core.tools import (
    READ_MAX_LINE_CHARS,
    ToolExecutionResult,
    ToolExecutor,
    Truncation,
    _format_size,
    parse_tool_arguments,
    truncate_text,
)


def assert_edit_ok(
    result: ToolExecutionResult,
    *,
    start_line: int,
    old_line_count: int,
    new_line_count: int,
) -> None:
    payload = json.loads(result.model_text)
    assert payload["status"] == "ok"
    assert payload["start_line"] == start_line
    assert payload["old_line_count"] == old_line_count
    assert payload["new_line_count"] == new_line_count


class TestTruncateText:
    """Tests for text truncation logic."""

    def test_no_truncation_needed(self):
        """Short text should not be truncated."""
        text = "Hello\nWorld"
        content, trunc = truncate_text(text, max_lines=10, max_bytes=1000)

        assert content == text
        assert trunc.truncated is False
        assert trunc.truncated_by is None

    def test_truncated_by_lines(self):
        """Text exceeding line limit should be truncated."""
        text = "\n".join([f"line {i}" for i in range(100)])
        content, trunc = truncate_text(text, max_lines=10, max_bytes=100000)

        assert trunc.truncated is True
        assert trunc.truncated_by == "lines"
        assert trunc.output_lines == 10
        assert "line 9" in content
        assert "line 10" not in content

    def test_truncated_by_bytes(self):
        """Text exceeding byte limit should be truncated."""
        text = "x" * 1000
        content, trunc = truncate_text(text, max_lines=1000, max_bytes=100)

        assert trunc.truncated is True
        assert trunc.truncated_by == "bytes"
        assert trunc.output_bytes <= 100

    def test_truncated_by_bytes_mid_line(self):
        """Byte truncation can happen mid-line."""
        lines = ["short", "a" * 1000, "another short"]
        text = "\n".join(lines)
        content, trunc = truncate_text(text, max_lines=10, max_bytes=50)

        assert trunc.truncated is True
        assert len(content.encode("utf-8")) <= 50 + 20  # some margin for newlines

    def test_empty_text(self):
        """Empty text should handle gracefully."""
        content, trunc = truncate_text("")

        assert content == ""
        assert trunc.truncated is False
        assert trunc.output_lines == 0
        assert trunc.output_bytes == 0

    def test_single_line(self):
        """Single line text should not be truncated."""
        content, trunc = truncate_text("single line")

        assert content == "single line"
        assert trunc.truncated is False
        assert trunc.output_lines == 1

    def test_tail_truncation_keeps_last_lines(self):
        text = "\n".join([f"line {i}" for i in range(20)])
        content, trunc = truncate_text(text, max_lines=5, max_bytes=1000, tail=True)

        assert trunc.truncated is True
        assert trunc.truncated_by == "lines"
        assert "line 19" in content
        assert "line 15" in content
        assert "line 14" not in content


class TestFormatSize:
    """Tests for human-readable size formatting."""

    def test_bytes(self):
        assert _format_size(0) == "0B"
        assert _format_size(500) == "500B"
        assert _format_size(1023) == "1023B"

    def test_kilobytes(self):
        assert _format_size(1024) == "1.0KB"
        assert _format_size(1536) == "1.5KB"
        assert _format_size(1024 * 1024 - 1) == "1024.0KB"

    def test_megabytes(self):
        assert _format_size(1024 * 1024) == "1.0MB"
        assert _format_size(1024 * 1024 * 5) == "5.0MB"


class TestParseToolArguments:
    """Tests for tool argument parsing."""

    def test_valid_json(self):
        result = parse_tool_arguments('{"path": "/tmp/file.txt"}')
        assert result == {"path": "/tmp/file.txt"}

    def test_empty_string(self):
        result = parse_tool_arguments("")
        assert result == {}

    def test_none(self):
        result = parse_tool_arguments(None)
        assert result == {}

    def test_invalid_json(self):
        result = parse_tool_arguments("not json")
        assert isinstance(result, str)
        assert "invalid" in result.lower()

    def test_non_object_json(self):
        result = parse_tool_arguments("[1, 2, 3]")
        assert isinstance(result, str)
        assert "object" in result.lower()


class TestToolExecutorRead:
    """Tests for ToolExecutor.read()."""

    def test_read_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = executor.read(path="test.txt")
            assert result.model_text == "Hello, World!"

    def test_read_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.read(path="nonexistent.txt")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "not found" in result.model_text.lower()

    def test_read_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            subdir = Path(tmpdir) / "subdir"
            subdir.mkdir()

            result = executor.read(path="subdir")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "not a file" in result.model_text.lower()

    def test_read_with_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2\nline 3\nline 4")

            result = executor.read(path="test.txt", offset=2, limit=2)
            assert "line 2" in result.model_text
            assert "line 3" in result.model_text
            assert "line 1" not in result.model_text
            assert "line 4" not in result.model_text

    def test_read_with_limit_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2\nline 3\nline 4")

            result = executor.read(path="test.txt", limit=2)
            assert "line 1" in result.model_text
            assert "line 2" in result.model_text
            assert "line 3" not in result.model_text

    def test_read_offset_beyond_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2")

            result = executor.read(path="test.txt", offset=10)
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "beyond" in result.model_text.lower()

    def test_read_binary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "binary.bin"
            # Use invalid UTF-8 sequence
            test_file.write_bytes(b"\x80\x81\x82\x83")

            result = executor.read(path="binary.bin")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "utf-8" in result.model_text.lower()

    def test_read_truncated_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "large.txt"
            # Create a file larger than the default line window
            lines = [f"line {i}" for i in range(3000)]
            test_file.write_text("\n".join(lines))

            result = executor.read(path="large.txt")
            assert "[Showing lines 1-2000. Use offset=2001 to continue.]" in result.model_text

    def test_read_shortens_long_line_and_adds_slice_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "long.txt"
            test_file.write_text("short\n" + ("x" * (READ_MAX_LINE_CHARS + 50)))

            result = executor.read(path="long.txt")

            assert "... [line truncated]" in result.model_text
            assert f"shortened to {READ_MAX_LINE_CHARS} chars" in result.model_text
            assert "sed -n '2p'" in result.model_text
            assert "head -c 2000" in result.model_text


class TestToolExecutorWrite:
    """Tests for ToolExecutor.write()."""

    def test_write_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.write(path="new.txt", content="Hello!")
            assert result.model_text == "ok"
            assert result.display_text == "Wrote new.txt"

            written = Path(tmpdir) / "new.txt"
            assert written.read_text() == "Hello!"

    def test_write_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "existing.txt"
            test_file.write_text("Old content")

            result = executor.write(path="existing.txt", content="New content")
            assert result.model_text == "ok"
            assert test_file.read_text() == "New content"

    def test_write_nested_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.write(path="subdir/nested/file.txt", content="Nested!")
            assert result.model_text == "ok"

            written = Path(tmpdir) / "subdir" / "nested" / "file.txt"
            assert written.exists()
            assert written.read_text() == "Nested!"


class TestToolExecutorEdit:
    """Tests for ToolExecutor.edit()."""

    def test_edit_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = executor.edit(path="test.txt", oldText="World", newText="Universe")
            assert_edit_ok(result, start_line=1, old_line_count=1, new_line_count=1)
            assert test_file.read_text() == "Hello, Universe!"

    def test_edit_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = executor.edit(path="test.txt", oldText="NotFound", newText="Replacement")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "not found" in result.model_text.lower()

    def test_edit_multiple_occurrences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("apple apple apple")

            result = executor.edit(path="test.txt", oldText="apple", newText="orange")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "occurs" in result.model_text.lower()
            # Should not have changed
            assert test_file.read_text() == "apple apple apple"

    def test_edit_exact_snippet_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello World")

            result = executor.edit(path="test.txt", oldText="Hello", newText="Hi")
            assert_edit_ok(result, start_line=1, old_line_count=1, new_line_count=1)
            assert test_file.read_text() == "Hi World"

    def test_edit_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.edit(path="nonexistent.txt", oldText="x", newText="y")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "not found" in result.model_text.lower()

    def test_edit_not_found_includes_closest_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("alpha\nbeta gamma\ndelta")

            result = executor.edit(path="test.txt", oldText="beta gamam", newText="replacement")
            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "closest line" in result.model_text.lower()
            assert "beta gamma" in result.model_text

    def test_edit_fuzzy_matches_trailing_whitespace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("def f():\n    return 1    \n")

            result = executor.edit(
                path="test.py",
                oldText="def f():\n    return 1\n",
                newText="def f():\n    return 2\n",
            )

            assert_edit_ok(result, start_line=1, old_line_count=2, new_line_count=2)
            assert test_file.read_text() == "def f():\n    return 2\n"

    def test_edit_fuzzy_matches_crlf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_bytes(b"line1\r\nline2\r\n")

            result = executor.edit(path="test.txt", oldText="line1\nline2\n", newText="line1\nlineX\n")

            assert_edit_ok(result, start_line=1, old_line_count=2, new_line_count=2)
            assert test_file.read_text(encoding="utf-8") == "line1\nlineX\n"

    def test_edit_fuzzy_requires_unique_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_bytes(b"x  \r\nx\t\r\n")

            result = executor.edit(path="test.txt", oldText="x\n", newText="y\n")

            assert result.is_error is True
            assert "error" in result.model_text.lower()
            assert "occurs" in result.model_text.lower()
            assert "normalization" in result.model_text.lower()


class TestToolExecutorAbsolutePath:
    """Tests for handling absolute vs relative paths."""

    def test_read_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd="/tmp", session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "abs_test.txt"
            test_file.write_text("Absolute path content")

            # Use absolute path
            result = executor.read(path=str(test_file))
            assert "Absolute path content" in result.model_text

    def test_read_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "rel_test.txt"
            test_file.write_text("Relative path content")

            # Use relative path
            result = executor.read(path="rel_test.txt")
            assert "Relative path content" in result.model_text

    def test_write_relative_path_resolves_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.write(path="relative.txt", content="Content")
            assert result.model_text == "ok"

            # File should be in cwd, not session_dir
            assert (Path(tmpdir) / "relative.txt").exists()
