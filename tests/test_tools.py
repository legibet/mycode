"""Basic tests for tool execution and truncation."""

import tempfile
from pathlib import Path

import pytest

from app.agent.tools import (
    ToolExecutor,
    Truncation,
    _format_size,
    parse_tool_arguments,
    truncate_text,
)


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
            assert result == "Hello, World!"

    def test_read_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.read(path="nonexistent.txt")
            assert "error" in result.lower()
            assert "not found" in result.lower()

    def test_read_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            subdir = Path(tmpdir) / "subdir"
            subdir.mkdir()

            result = executor.read(path="subdir")
            assert "error" in result.lower()
            assert "not a file" in result.lower()

    def test_read_with_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2\nline 3\nline 4")

            result = executor.read(path="test.txt", offset=2, limit=2)
            assert "line 2" in result
            assert "line 3" in result
            assert "line 1" not in result
            assert "line 4" not in result

    def test_read_with_limit_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2\nline 3\nline 4")

            result = executor.read(path="test.txt", limit=2)
            assert "line 1" in result
            assert "line 2" in result
            assert "line 3" not in result

    def test_read_offset_beyond_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("line 1\nline 2")

            result = executor.read(path="test.txt", offset=10)
            assert "error" in result.lower()
            assert "beyond" in result.lower()

    def test_read_binary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "binary.bin"
            # Use invalid UTF-8 sequence
            test_file.write_bytes(b"\x80\x81\x82\x83")

            result = executor.read(path="binary.bin")
            assert "error" in result.lower()
            assert "utf-8" in result.lower()

    def test_read_truncated_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "large.txt"
            # Create a file larger than default truncation limits
            lines = [f"line {i}" for i in range(3000)]
            test_file.write_text("\n".join(lines))

            result = executor.read(path="large.txt")
            assert "truncated" in result.lower() or "Showing lines" in result
            assert "offset=" in result


class TestToolExecutorWrite:
    """Tests for ToolExecutor.write()."""

    def test_write_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.write(path="new.txt", content="Hello!")
            assert result == "ok"

            written = Path(tmpdir) / "new.txt"
            assert written.read_text() == "Hello!"

    def test_write_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "existing.txt"
            test_file.write_text("Old content")

            result = executor.write(path="existing.txt", content="New content")
            assert result == "ok"
            assert test_file.read_text() == "New content"

    def test_write_nested_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.write(path="subdir/nested/file.txt", content="Nested!")
            assert result == "ok"

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
            assert result == "ok"
            assert test_file.read_text() == "Hello, Universe!"

    def test_edit_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = executor.edit(path="test.txt", oldText="NotFound", newText="Replacement")
            assert "error" in result.lower()
            assert "not found" in result.lower()

    def test_edit_multiple_occurrences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("apple apple apple")

            result = executor.edit(path="test.txt", oldText="apple", newText="orange")
            assert "error" in result.lower()
            assert "occurs" in result.lower()
            # Should not have changed
            assert test_file.read_text() == "apple apple apple"

    def test_edit_exact_match_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello World")

            # Partial match should not work
            result = executor.edit(path="test.txt", oldText="Hello", newText="Hi")
            assert result == "ok"
            assert test_file.read_text() == "Hi World"

    def test_edit_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.edit(path="nonexistent.txt", oldText="x", newText="y")
            assert "error" in result.lower()
            assert "not found" in result.lower()

    def test_edit_not_found_includes_closest_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("alpha\nbeta gamma\ndelta")

            result = executor.edit(path="test.txt", oldText="beta gamam", newText="replacement")
            assert "error" in result.lower()
            assert "closest line" in result.lower()
            assert "beta gamma" in result


class TestToolExecutorAbsolutePath:
    """Tests for handling absolute vs relative paths."""

    def test_read_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd="/tmp", session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "abs_test.txt"
            test_file.write_text("Absolute path content")

            # Use absolute path
            result = executor.read(path=str(test_file))
            assert "Absolute path content" in result

    def test_read_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "rel_test.txt"
            test_file.write_text("Relative path content")

            # Use relative path
            result = executor.read(path="rel_test.txt")
            assert "Relative path content" in result

    def test_write_relative_path_resolves_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ToolExecutor(cwd=tmpdir, session_dir=Path(tmpdir))

            result = executor.write(path="relative.txt", content="Content")
            assert result == "ok"

            # File should be in cwd, not session_dir
            assert (Path(tmpdir) / "relative.txt").exists()
