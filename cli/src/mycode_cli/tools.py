"""The CLI's built-in tools: ``read`` / ``write`` / ``edit`` / ``bash``.

Regular :class:`ToolSpec` values built on the SDK tool runtime. ``bash``
streams live output through ``ctx.emit``, spills large output to
``ctx.tool_output_dir``, and on cancellation kills its process group and
returns the captured partial output.
"""

from __future__ import annotations

import asyncio
import codecs
import locale
import os
import shlex
import signal
from base64 import b64encode
from contextlib import suppress
from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from mycode.attachments import detect_image_mime_type
from mycode.messages import image_block, text_block
from mycode.tools import ToolContext, ToolExecutionResult, ToolSpec, tool
from mycode.utils import resolve_path

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
READ_MAX_LINE_CHARS = 2000
BASH_TIMEOUT_SECONDS = 120
_BASH_READ_CHUNK_SIZE = 64 * 1024


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@tool(
    name="read",
    description=(
        "Read a UTF-8 text file or supported image. Use offset and limit to read large text files in sections."
    ),
    parameters={
        "path": "File path, relative to the working directory or absolute.",
        "offset": "1-indexed starting line. Defaults to 1.",
        "limit": "Maximum number of lines to return. Defaults to 2000.",
    },
)
def read_tool(
    ctx: ToolContext,
    path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> ToolExecutionResult:
    """Read a UTF-8 text file or a supported image file.

    Offset is 1-indexed. Limit caps the number of lines returned.
    """

    file_path = resolve_path(path, cwd=ctx.cwd)

    image_mime_type = detect_image_mime_type(file_path)
    if image_mime_type:
        if not ctx.supports_image_input:
            return ToolExecutionResult(
                output="error: image input is not supported by the current model",
                is_error=True,
            )
        summary = f"Read image file [{image_mime_type}]"
        try:
            image_data = b64encode(file_path.read_bytes()).decode("utf-8")
        except FileNotFoundError:
            return ToolExecutionResult(output=f"error: file not found: {path}", is_error=True)
        except Exception as exc:
            return ToolExecutionResult(output=f"error: failed to read file: {exc}", is_error=True)
        return ToolExecutionResult(
            output=summary,
            content=[
                text_block(summary),
                image_block(image_data, mime_type=image_mime_type, name=file_path.name),
            ],
        )

    start_line = offset if offset is not None and offset > 0 else 1
    line_limit = limit if limit is not None and limit > 0 else DEFAULT_MAX_LINES
    lines: list[str] = []
    total_lines = 0
    next_offset: int | None = None
    first_shortened_line: int | None = None
    shortened_lines = 0

    try:
        with file_path.open("r", encoding="utf-8") as f:
            for total_lines, raw_line in enumerate(f, start=1):
                if total_lines < start_line:
                    continue
                if len(lines) >= line_limit:
                    next_offset = total_lines
                    break

                line = raw_line.rstrip("\r\n")
                if len(line) > READ_MAX_LINE_CHARS:
                    if first_shortened_line is None:
                        first_shortened_line = total_lines
                    shortened_lines += 1
                    line = line[:READ_MAX_LINE_CHARS] + " ... [line truncated]"
                lines.append(line)
    except FileNotFoundError:
        return ToolExecutionResult(output=f"error: file not found: {path}", is_error=True)
    except IsADirectoryError:
        return ToolExecutionResult(output=f"error: not a file: {path}", is_error=True)
    except UnicodeDecodeError:
        return ToolExecutionResult(output=f"error: file is not valid utf-8 text: {path}", is_error=True)
    except Exception as exc:
        return ToolExecutionResult(output=f"error: failed to read file: {exc}", is_error=True)

    if total_lines < start_line and not (total_lines == 0 and start_line == 1):
        return ToolExecutionResult(
            output=f"error: offset {offset} beyond end of file ({total_lines} lines)",
            is_error=True,
        )

    parts: list[str] = []
    content = "\n".join(lines)
    if content:
        parts.append(content)

    if next_offset is not None:
        parts.append(f"[Showing lines {start_line}-{next_offset - 1}. Use offset={next_offset} to continue.]")

    if first_shortened_line is not None:
        quoted = shlex.quote(str(file_path))
        prefix = f"[Line {first_shortened_line} was shortened to {READ_MAX_LINE_CHARS} chars."
        if shortened_lines > 1:
            prefix = (
                f"[{shortened_lines} lines were shortened to {READ_MAX_LINE_CHARS} chars. "
                f"First shortened line: {first_shortened_line}."
            )
        parts.append(
            f"{prefix}\n"
            + "Use bash to inspect it in bytes:\n"
            + f"sed -n '{first_shortened_line}p' {quoted} | head -c 2000\n"
            + f"sed -n '{first_shortened_line}p' {quoted} | tail -c +2001 | head -c 2000]"
        )

    joined = "\n\n".join(parts) if parts else ""
    return ToolExecutionResult(output=joined)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str, *, newline: str | None = None) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    if newline is None:
        tmp.write_text(content, encoding="utf-8")
    else:
        normalized = content.replace("\r\n", "\n")
        if newline == "\r\n":
            normalized = normalized.replace("\n", "\r\n")
        with tmp.open("w", encoding="utf-8", newline="") as file:
            file.write(normalized)
    tmp.replace(path)


@tool(
    name="write",
    description=(
        "Create or completely overwrite a UTF-8 text file, creating parent directories as needed. "
        "Use edit for targeted changes to existing files."
    ),
    parameters={
        "path": "File path, relative to the working directory or absolute.",
        "content": "File content.",
    },
)
def write_tool(ctx: ToolContext, path: str, content: str) -> ToolExecutionResult:
    file_path = resolve_path(path, cwd=ctx.cwd)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(file_path, content)
    except Exception as exc:
        return ToolExecutionResult(output=f"error: failed to write file: {exc}", is_error=True)
    return ToolExecutionResult(output=f"Wrote {path}")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


class EditEntry(BaseModel):
    """One text replacement for the edit tool."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)

    old_text: str = Field(alias="oldText", description="Text to replace; it must be unique in the original file.")
    new_text: str = Field(alias="newText", description="Replacement text. Empty text deletes the match.")


def _closest_line_hint(text: str, needle: str) -> str | None:
    needle_clean = needle.strip()
    if not needle_clean:
        return None

    best_ratio = 0.0
    best_line = ""
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        ratio = SequenceMatcher(None, needle_clean, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = candidate
            if ratio >= 1.0:
                break

    if best_ratio < 0.6 or not best_line:
        return None

    if len(best_line) > 120:
        return best_line[:117] + "..."
    return best_line


def _normalize_text(text: str) -> tuple[str, list[int]]:
    """Normalize text while preserving a map back to original offsets."""

    chars: list[str] = []
    imap: list[int] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        trimmed = content.rstrip(" \t")
        chars.extend(trimmed)
        imap.extend(range(pos, pos + len(trimmed)))
        eol = line[len(content) :]
        if eol:
            chars.append("\n")
            imap.append(pos + len(content))
        pos += len(line)

    return "".join(chars), imap


@tool(
    name="edit",
    description=(
        "Apply one or more non-overlapping replacements to a UTF-8 text file. "
        "Each oldText must identify one unique region in the original file, "
        "and all replacements are applied together. "
        "For multiple disjoint changes in one file, use one call with multiple edits."
    ),
    parameters={
        "path": "File path, relative to the working directory or absolute.",
        "edits": "Replacements to apply.",
    },
)
def edit_tool(ctx: ToolContext, path: str, edits: list[EditEntry]) -> ToolExecutionResult:
    """Replace one or more unique snippets in a file.

    Edits all match against the original file content. Exact match is tried
    first; a conservative fuzzy fallback tolerates line-ending and trailing-
    whitespace differences while only replacing the matched region.
    """

    file_path = resolve_path(path, cwd=ctx.cwd)
    if not edits:
        return ToolExecutionResult(output="error: edits must not be empty", is_error=True)

    multi = len(edits) > 1
    for i, entry in enumerate(edits):
        old_text = entry.old_text
        new_text = entry.new_text
        pfx = f"edits[{i}]: " if multi else ""
        if not old_text:
            return ToolExecutionResult(output=f"error: {pfx}oldText must not be empty", is_error=True)
        if old_text == new_text:
            return ToolExecutionResult(output=f"error: {pfx}oldText and newText are identical", is_error=True)

    try:
        read_mtime_ns = file_path.stat().st_mtime_ns
        with file_path.open("r", encoding="utf-8", newline="") as file:
            text = file.read()
    except FileNotFoundError:
        return ToolExecutionResult(output=f"error: file not found: {path}", is_error=True)
    except IsADirectoryError:
        return ToolExecutionResult(output=f"error: not a file: {path}", is_error=True)
    except Exception as exc:
        return ToolExecutionResult(output=f"error: failed to read file: {exc}", is_error=True)

    newline = "\r\n" if "\r\n" in text else None

    matches: list[tuple[int, int, str, int]] = []
    norm_text: str | None = None
    norm_imap: list[int] | None = None

    for i, entry in enumerate(edits):
        old_text = entry.old_text
        new_text = entry.new_text
        pfx = f"edits[{i}]: " if multi else ""

        exact_count = text.count(old_text)
        if exact_count == 1:
            pos = text.index(old_text)
            matches.append((pos, pos + len(old_text), new_text, i))
            continue
        if exact_count > 1:
            return ToolExecutionResult(
                output=f"error: {pfx}oldText occurs {exact_count} times; provide a more specific oldText",
                is_error=True,
            )

        if norm_text is None:
            norm_text, norm_imap = _normalize_text(text)
        norm_old, _ = _normalize_text(old_text)

        norm_count = norm_text.count(norm_old)
        if norm_count == 0:
            hint = _closest_line_hint(text, old_text)
            msg = f"error: {pfx}oldText not found"
            if hint:
                msg += f". closest line: {hint}"
            return ToolExecutionResult(output=msg, is_error=True)
        if norm_count > 1:
            return ToolExecutionResult(
                output=(
                    f"error: {pfx}oldText occurs {norm_count} times after normalization; "
                    "provide a more specific oldText"
                ),
                is_error=True,
            )

        idx = norm_text.find(norm_old)
        assert norm_imap is not None
        # Normalized matching tolerates whitespace drift; replacement still
        # uses original offsets so untouched content is preserved exactly.
        orig_start = norm_imap[idx]
        end_idx = idx + len(norm_old)
        orig_end = norm_imap[end_idx] if end_idx < len(norm_imap) else len(text)
        matches.append((orig_start, orig_end, new_text, i))

    matches.sort(key=lambda m: m[0])
    for j in range(1, len(matches)):
        _, prev_end, _, prev_i = matches[j - 1]
        curr_start, _, _, curr_i = matches[j]
        if prev_end > curr_start:
            return ToolExecutionResult(
                output=f"error: edits[{prev_i}] and edits[{curr_i}] overlap",
                is_error=True,
            )

    # Apply replacements back-to-front so earlier offsets stay valid.
    updated = text
    for start, end, new_text, _ in reversed(matches):
        updated = updated[:start] + new_text + updated[end:]

    if updated == text:
        return ToolExecutionResult(output="error: edits produced no changes", is_error=True)

    try:
        if file_path.stat().st_mtime_ns != read_mtime_ns:
            return ToolExecutionResult(
                output="error: file changed while editing; read it again and retry",
                is_error=True,
            )
        _atomic_write_text(file_path, updated, newline=newline)
    except Exception as exc:
        return ToolExecutionResult(output=f"error: failed to write file: {exc}", is_error=True)

    # The UI renders this standard patch directly; stats are counted from the
    # same patch so TUI and web show the same +N/-N numbers.
    patch_lines = list(
        unified_diff(
            text.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="",
        )
    )
    patch = "\n".join(patch_lines)
    if patch:
        patch += "\n"
    patch_body = patch_lines[2:]
    added_lines = sum(1 for line in patch_body if line.startswith("+"))
    removed_lines = sum(1 for line in patch_body if line.startswith("-"))

    summary = f"Updated {path}" if len(edits) == 1 else f"Updated {path} ({len(edits)} edits)"
    return ToolExecutionResult(
        output=summary,
        metadata={
            "patch": patch,
            "added_lines": added_lines,
            "removed_lines": removed_lines,
        },
    )


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Truncation:
    # "lines" or "bytes"; None when nothing was cut.
    truncated_by: str | None
    output_lines: int


def truncate_tail(text: str) -> tuple[str, Truncation]:
    """Keep the trailing lines that fit the display line and byte limits."""

    raw_lines = text.splitlines(keepends=True)
    lines = [raw_line.rstrip("\r\n") for raw_line in raw_lines]
    out_lines: list[str] = []
    out_bytes = 0
    sliced = False

    for line, raw_line in zip(reversed(lines), reversed(raw_lines), strict=True):
        if len(out_lines) >= DEFAULT_MAX_LINES:
            break
        line_bytes = len(raw_line.encode("utf-8"))
        if out_bytes + line_bytes > DEFAULT_MAX_BYTES:
            # An oversized line is sliced rather than dropped whole, so the
            # result carries the full byte budget even when one line dominates.
            budget = DEFAULT_MAX_BYTES - out_bytes
            if budget > 0:
                encoded = line.encode("utf-8")
                out_lines.append(encoded[-budget:].decode("utf-8", errors="ignore"))
                sliced = True
            break
        out_lines.append(line)
        out_bytes += line_bytes

    out_lines.reverse()
    content = "\n".join(out_lines)

    truncated_by: str | None
    if sliced:
        truncated_by = "bytes"
    elif len(out_lines) == len(lines):
        truncated_by = None
    elif len(out_lines) == DEFAULT_MAX_LINES:
        truncated_by = "lines"
    else:
        truncated_by = "bytes"

    return content, Truncation(truncated_by=truncated_by, output_lines=len(out_lines))


@dataclass(frozen=True)
class _BashOutputSnapshot:
    content: str
    truncation: Truncation
    total_lines: int
    last_line_partial: bool
    full_output_path: Path | None


class _BashOutputAccumulator:
    """Capture raw command output while keeping a bounded display tail."""

    def __init__(self, tool_output_dir: Path, log_path: Path):
        self.tool_output_dir = tool_output_dir
        self.log_path = log_path
        encoding = locale.getpreferredencoding(False)
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.raw_chunks: list[bytes] = []
        self.tail_text = ""
        self.tail_bytes = 0
        self.total_raw_bytes = 0
        self.total_bytes = 0
        self.completed_lines = 0
        self.current_line_bytes = 0
        self.last_completed_line_bytes = 0
        self.has_open_line = False
        self.log_file: BinaryIO | None = None
        self.finished = False

    def append(self, chunk: bytes) -> str:
        if self.finished:
            raise RuntimeError("cannot append after bash output is finished")

        self.total_raw_bytes += len(chunk)
        text = self.decoder.decode(chunk)
        self._append_text(text)

        if self.log_file is None:
            self.raw_chunks.append(chunk)
            if self._needs_full_log():
                self._open_log()
        else:
            self.log_file.write(chunk)
        return text

    def finish(self) -> str:
        if self.finished:
            return ""

        self.finished = True
        text = self.decoder.decode(b"", final=True)
        self._append_text(text)
        if self.log_file is None and self._needs_full_log():
            self._open_log()
        return text

    def snapshot(self) -> _BashOutputSnapshot:
        content, truncation = truncate_tail(self.tail_text)
        truncated = self._needs_full_log() or truncation.truncated_by is not None
        truncated_by = truncation.truncated_by
        if truncated and truncated_by is None:
            truncated_by = (
                "bytes" if self.total_raw_bytes > DEFAULT_MAX_BYTES or self.total_bytes > DEFAULT_MAX_BYTES else "lines"
            )
            truncation = Truncation(truncated_by=truncated_by, output_lines=truncation.output_lines)

        last_line_bytes = self.current_line_bytes if self.has_open_line else self.last_completed_line_bytes
        return _BashOutputSnapshot(
            content=content,
            truncation=truncation,
            total_lines=self.completed_lines + int(self.has_open_line),
            last_line_partial=last_line_bytes > DEFAULT_MAX_BYTES and truncated_by == "bytes",
            full_output_path=self.log_path if self.log_file is not None else None,
        )

    def close(self) -> None:
        if self.log_file is not None:
            with suppress(Exception):
                self.log_file.close()
            self.log_file = None

    def _append_text(self, text: str) -> None:
        if not text:
            return

        encoded_bytes = len(text.encode("utf-8"))
        self.total_bytes += encoded_bytes
        self.tail_text += text
        self.tail_bytes += encoded_bytes
        if self.tail_bytes > DEFAULT_MAX_BYTES * 4:
            self._trim_tail()

        segments = text.split("\n")
        for segment in segments[:-1]:
            self.current_line_bytes += len(segment.encode("utf-8"))
            self.completed_lines += 1
            self.last_completed_line_bytes = self.current_line_bytes
            self.current_line_bytes = 0
        trailing = segments[-1]
        if len(segments) > 1:
            self.current_line_bytes = len(trailing.encode("utf-8"))
        else:
            self.current_line_bytes += len(trailing.encode("utf-8"))
        self.has_open_line = bool(trailing)

    def _trim_tail(self) -> None:
        encoded = self.tail_text.encode("utf-8")
        if len(encoded) <= DEFAULT_MAX_BYTES * 2:
            self.tail_bytes = len(encoded)
            return

        start = len(encoded) - DEFAULT_MAX_BYTES * 2
        while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
            start += 1
        self.tail_text = encoded[start:].decode("utf-8", errors="replace")
        self.tail_bytes = len(self.tail_text.encode("utf-8"))

    def _needs_full_log(self) -> bool:
        return (
            self.total_raw_bytes > DEFAULT_MAX_BYTES
            or self.total_bytes > DEFAULT_MAX_BYTES
            or self.completed_lines + int(self.has_open_line) > DEFAULT_MAX_LINES
        )

    def _open_log(self) -> None:
        self.tool_output_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_path.open("wb")
        for chunk in self.raw_chunks:
            self.log_file.write(chunk)
        self.raw_chunks = []


def _format_bash_output(snapshot: _BashOutputSnapshot) -> str:
    result = snapshot.content.strip() or "(empty)"
    truncated_by = snapshot.truncation.truncated_by
    if truncated_by is None:
        return result

    if snapshot.last_line_partial:
        summary = "Showing the last 50KB of the final output line."
        action = "Use Bash byte-range commands to inspect the complete line."
    elif truncated_by == "lines":
        summary = f"Showing the last {snapshot.truncation.output_lines} of {snapshot.total_lines} lines."
        action = "Use read with offset to inspect earlier lines."
    else:
        summary = "Showing the last 50KB of output."
        action = "Use read to inspect omitted output."

    path = f" Full output: {snapshot.full_output_path}." if snapshot.full_output_path is not None else ""
    return f"{result}\n\n[Output truncated: {summary}{path} {action}]"


def _cancelled_bash_result(snapshot: _BashOutputSnapshot) -> ToolExecutionResult:
    return ToolExecutionResult(
        output=f"{_format_bash_output(snapshot)}\n\nerror: cancelled",
        is_error=True,
    )


def _kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        with suppress(Exception):
            proc.kill()


@tool(
    name="bash",
    description=(
        "Run a shell command in the session working directory. "
        "Large output returns the tail and saves the full log to a file."
    ),
    parameters={
        "command": "Shell command.",
        "timeout": "Timeout in seconds. Defaults to 120.",
    },
    streams_output=True,
)
async def bash_tool(
    ctx: ToolContext,
    command: str,
    timeout: int | None = None,  # noqa: ASYNC109
) -> ToolExecutionResult:
    """Run a shell command and return combined stdout/stderr text."""

    timeout_seconds = timeout if timeout is not None and timeout > 0 else BASH_TIMEOUT_SECONDS
    proc: asyncio.subprocess.Process | None = None
    log_path = ctx.tool_output_dir / f"bash-{ctx.tool_call_id or 'call'}.log"
    output = _BashOutputAccumulator(ctx.tool_output_dir, log_path)

    def emit_output(text: str) -> None:
        if text and ctx.emit is not None:
            ctx.emit(text)

    async def drain_stdout() -> None:
        assert proc is not None
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(_BASH_READ_CHUNK_SIZE):
            emit_output(output.append(chunk))

    async def terminate_and_drain() -> None:
        if proc is None:
            return
        _kill_proc_tree(proc)
        with suppress(TimeoutError):
            async with asyncio.timeout(1):
                await drain_stdout()
                await proc.wait()

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=ctx.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=_BASH_READ_CHUNK_SIZE,
            start_new_session=os.name == "posix",
        )

        try:
            async with asyncio.timeout(timeout_seconds):
                await drain_stdout()
                emit_output(output.finish())
                await proc.wait()
        except TimeoutError:
            await terminate_and_drain()
            emit_output(output.finish())
            return ToolExecutionResult(
                output=f"{_format_bash_output(output.snapshot())}\n\n[Command timed out after {timeout_seconds}s]",
                is_error=True,
            )

        result = _format_bash_output(output.snapshot())
        exit_code = proc.returncode
        if exit_code:
            result += f"\n\n[exit code: {exit_code}]"
        return ToolExecutionResult(output=result, is_error=bool(exit_code))

    except asyncio.CancelledError:
        # The cancellation ends here: uncancel so terminate_and_drain's
        # asyncio.timeout is not re-interrupted by the stale cancel request,
        # then return the partial output like any other tool result.
        task = asyncio.current_task()
        assert task is not None
        task.uncancel()
        await terminate_and_drain()
        emit_output(output.finish())
        return _cancelled_bash_result(output.snapshot())
    except Exception as exc:
        return ToolExecutionResult(output=f"error: {exc}", is_error=True)
    finally:
        output.close()
        if proc is not None:
            if proc.returncode is None:
                _kill_proc_tree(proc)
            with suppress(asyncio.CancelledError, Exception):
                await proc.wait()


DEFAULT_TOOLS: list[ToolSpec] = [read_tool, write_tool, edit_tool, bash_tool]
