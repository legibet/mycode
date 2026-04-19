"""Basic tests for tool execution and truncation."""

import base64
import tempfile
from pathlib import Path

from mycode.tools import (
    DEFAULT_TOOL_SPECS,
    READ_MAX_LINE_CHARS,
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    detect_image_mime_type,
    truncate_text,
)
from mycode.utils import parse_tool_arguments

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1X8AAAAASUVORK5CYII="
)


def _ctx(cwd: str, *, tool_output_dir: Path | None = None, supports_image_input: bool = False) -> ToolContext:
    """Build a ToolContext with the four built-ins registered."""

    executor = ToolExecutor(DEFAULT_TOOL_SPECS)
    return ToolContext(
        executor=executor,
        cwd=cwd,
        tool_output_dir=tool_output_dir if tool_output_dir is not None else Path(cwd),
        supports_image_input=supports_image_input,
    )


def assert_edit_ok(
    result: ToolExecutionResult,
    *,
    start_line: int | list[int],
    old_line_count: int | list[int],
    new_line_count: int | list[int],
) -> None:
    """Verify a successful edit result.

    Accepts scalar values (checked against the first/only edit) or lists
    (checked element-wise against each edit in the result).
    """
    assert result.is_error is False
    assert result.metadata is not None
    edits = result.metadata["edits"]
    assert isinstance(edits, list) and len(edits) > 0

    starts = [start_line] if isinstance(start_line, int) else start_line
    olds = [old_line_count] if isinstance(old_line_count, int) else old_line_count
    news = [new_line_count] if isinstance(new_line_count, int) else new_line_count
    assert len(edits) == len(starts) == len(olds) == len(news)

    for i, edit in enumerate(edits):
        assert edit["start_line"] == starts[i]
        assert edit["old_line_count"] == olds[i]
        assert edit["new_line_count"] == news[i]


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
        _, trunc = truncate_text(text, max_lines=1000, max_bytes=100)

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


def test_detect_image_mime_type_from_header(tmp_path):
    image_path = tmp_path / "tiny.bin"
    image_path.write_bytes(_PNG_1X1)

    assert detect_image_mime_type(image_path) == "image/png"


class TestWrite:
    def test_write_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir).write("new.txt", "Hello!")
            assert result.is_error is False
            assert result.output == "Wrote new.txt"

            assert (Path(tmpdir) / "new.txt").read_text() == "Hello!"

    def test_write_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "existing.txt"
            test_file.write_text("Old content")

            result = _ctx(tmpdir).write("existing.txt", "New content")
            assert result.is_error is False
            assert test_file.read_text() == "New content"

    def test_write_nested_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir).write("subdir/nested/file.txt", "Nested!")
            assert result.is_error is False

            written = Path(tmpdir) / "subdir" / "nested" / "file.txt"
            assert written.exists()
            assert written.read_text() == "Nested!"


class TestEdit:
    def test_edit_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "World", "newText": "Universe"}])
            assert_edit_ok(result, start_line=1, old_line_count=1, new_line_count=1)
            assert test_file.read_text() == "Hello, Universe!"

    def test_edit_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("Hello, World!")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "NotFound", "newText": "Replacement"}])
            assert result.is_error is True
            assert "not found" in result.output.lower()

    def test_edit_multiple_occurrences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("apple apple apple")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "apple", "newText": "orange"}])
            assert result.is_error is True
            assert "occurs" in result.output.lower()
            assert test_file.read_text() == "apple apple apple"

    def test_edit_exact_snippet_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello World")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "Hello", "newText": "Hi"}])
            assert_edit_ok(result, start_line=1, old_line_count=1, new_line_count=1)
            assert test_file.read_text() == "Hi World"

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
            assert_edit_ok(result, start_line=1, old_line_count=2, new_line_count=2)
            assert test_file.read_text() == "def f():\n    return 2\n"

    def test_edit_fuzzy_matches_crlf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_bytes(b"line1\r\nline2\r\n")

            result = _ctx(tmpdir).edit(
                "test.txt",
                [{"oldText": "line1\nline2\n", "newText": "line1\nlineX\n"}],
            )
            assert_edit_ok(result, start_line=1, old_line_count=2, new_line_count=2)
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
            assert_edit_ok(
                result,
                start_line=[1, 3],
                old_line_count=[1, 1],
                new_line_count=[1, 1],
            )
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

    def test_multi_edit_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("aaa bbb aaa")

            result = _ctx(tmpdir).edit("test.txt", [{"oldText": "aaa", "newText": "XXX"}])
            assert result.is_error is True
            assert "occurs" in result.output.lower()

    def test_multi_edit_empty_list_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("content")

            result = _ctx(tmpdir).edit("test.txt", [])
            assert result.is_error is True
            assert "empty" in result.output.lower()

    def test_multi_edit_fuzzy_preserves_untouched_regions(self):
        """Fuzzy match must not alter trailing whitespace in untouched lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            # Line 2 has trailing spaces that should be preserved.
            test_file.write_text("def f():\n    x = 1    \n    return x\n")

            result = _ctx(tmpdir).edit(
                "test.py",
                [{"oldText": "return x", "newText": "return x + 1"}],
            )
            assert_edit_ok(result, start_line=3, old_line_count=1, new_line_count=1)
            content = test_file.read_text()
            assert "    x = 1    \n" in content
            assert "return x + 1" in content

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
            assert_edit_ok(
                result,
                start_line=[1, 4],  # "c" is now line 4 because "a" expanded to 2 lines
                old_line_count=[1, 1],
                new_line_count=[2, 1],
            )
            assert test_file.read_text() == "a1\na2\nb\nC\n"

    def test_edit_added_removed_line_stats(self):
        """``added_lines`` / ``removed_lines`` in metadata reflect a real diff.

        These numbers drive the shared ``+N −M`` indicator in both TUI and
        web — they must match ``difflib.SequenceMatcher`` semantics so the
        two surfaces agree.
        """

        cases = [
            # single-line replace
            ("foo\n", "foo", "bar", 1, 1),
            # pure insert (one line becomes three, two new)
            ("foo\n", "foo", "foo\nbar\nbaz", 2, 0),
            # pure delete
            ("a\nb\nc\n", "a\nb\nc", "a\nc", 0, 1),
            # multi-line replace with shared prefix/suffix
            ("x\nold1\nold2\ny\n", "old1\nold2", "new1\nnew2\nnew3", 3, 2),
            # reorder — SequenceMatcher keeps one common line
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
                edit = result.metadata["edits"][0]
                assert edit["added_lines"] == expected_added, (old_text, new_text, edit)
                assert edit["removed_lines"] == expected_removed, (old_text, new_text, edit)


class TestAbsolutePath:
    def test_read_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "abs_test.txt"
            test_file.write_text("Absolute path content")

            # cwd elsewhere; reading by absolute path must still work.
            result = _ctx("/tmp").read(str(test_file))
            assert "Absolute path content" in result.output

    def test_read_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "rel_test.txt").write_text("Relative path content")

            result = _ctx(tmpdir).read("rel_test.txt")
            assert "Relative path content" in result.output

    def test_write_relative_path_resolves_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ctx(tmpdir).write("relative.txt", "Content")
            assert result.is_error is False

            assert (Path(tmpdir) / "relative.txt").exists()


# ``ToolExecutionResult`` constructor shape sanity check.
def test_tool_execution_result_fields():
    result = ToolExecutionResult(output="ok", metadata={"k": 1})
    assert result.output == "ok"
    assert result.metadata == {"k": 1}
    assert result.content is None
    assert result.is_error is False
