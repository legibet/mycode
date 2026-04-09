"""Core tool definitions and execution.

The runtime intentionally exposes only four built-in tools: ``read``,
``write``, ``edit``, and ``bash``.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from base64 import b64encode
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from mimetypes import guess_type
from pathlib import Path
from typing import Any, TextIO, cast

from mycode.core.messages import image_block, text_block

# ---------------------------------------------------------------------------
# Limits (keep token usage low)
# ---------------------------------------------------------------------------
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
READ_MAX_LINE_CHARS = 2000

BASH_TIMEOUT_SECONDS = 120
_BASH_MAX_IN_MEMORY_BYTES = 5_000_000


@dataclass(frozen=True)
class ToolSpec:
    """Built-in tool metadata and executor binding."""

    name: str
    description: str
    input_schema: dict[str, Any]
    method_name: str
    streams_output: bool = False


@dataclass(frozen=True)
class ToolExecutionResult:
    """Structured tool result used by the runtime.

    `model_text` is appended to session history for future provider replay.
    `display_text` is shown to the user.
    """

    model_text: str
    display_text: str
    is_error: bool = False
    content: list[dict[str, Any]] | None = None


DEFAULT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read",
        description=(
            "Read a UTF-8 text file or supported image file. Returns up to 2000 lines for text files. "
            "Use offset/limit for large files. Very long lines are shortened."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "offset": {"type": "integer", "description": "Line number to start from (1-indexed)."},
                "limit": {"type": "integer", "description": "Maximum number of lines to return."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        method_name="read",
    ),
    ToolSpec(
        name="write",
        description="Write a file (create or overwrite).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "content": {"type": "string", "description": "File content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        method_name="write",
    ),
    ToolSpec(
        name="edit",
        description=(
            "Edit a file by replacing text snippets. "
            "Each edits[].oldText must match uniquely in the original file. "
            "For multiple disjoint changes in one file, use one call with multiple edits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {
                                "type": "string",
                                "description": "Exact text to find (must be unique in the file).",
                            },
                            "newText": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                        },
                        "required": ["oldText", "newText"],
                        "additionalProperties": False,
                    },
                    "description": "Replacements to apply. All matched against the original file, not incrementally.",
                },
            },
            "required": ["path", "edits"],
            "additionalProperties": False,
        },
        method_name="edit",
    ),
    ToolSpec(
        name="bash",
        description=(
            "Run a shell command in the session working directory. "
            "Large output returns the tail and saves the full log to a file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (optional)."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        method_name="bash",
        streams_output=True,
    ),
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Truncation:
    truncated: bool
    truncated_by: str | None
    output_lines: int
    output_bytes: int


def truncate_text(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    tail: bool = False,
) -> tuple[str, Truncation]:
    """Truncate text by both line and byte limits.

    Returns (content, truncation).
    """

    lines = text.splitlines()
    out_lines: list[str] = []
    out_bytes = 0

    source = reversed(lines) if tail else lines

    for line in source:
        if len(out_lines) >= max_lines:
            break
        # +1 for newline when joined later
        b = len((line + "\n").encode("utf-8"))
        if out_bytes + b > max_bytes:
            break
        out_lines.append(line)
        out_bytes += b

    if tail:
        out_lines.reverse()

    # Edge case: a single line exceeds max_bytes — take the tail/head slice
    if not out_lines and lines:
        target = lines[-1] if tail else lines[0]
        encoded = target.encode("utf-8")
        sliced = encoded[-max_bytes:] if tail else encoded[:max_bytes]
        content = sliced.decode("utf-8", errors="ignore")
        return content, Truncation(
            truncated=True,
            truncated_by="bytes",
            output_lines=1,
            output_bytes=len(content.encode("utf-8")),
        )

    content = "\n".join(out_lines)
    truncated = len(out_lines) < len(lines) or out_bytes < len(text.encode("utf-8"))

    truncated_by: str | None = None
    if truncated:
        if len(out_lines) < len(lines):
            truncated_by = "lines" if len(out_lines) == max_lines else "bytes"
        else:
            truncated_by = "bytes"

    trunc = Truncation(
        truncated=truncated,
        truncated_by=truncated_by,
        output_lines=len(out_lines),
        output_bytes=len(content.encode("utf-8")),
    )
    return content, trunc


def resolve_path(path: str, *, cwd: str) -> str:
    """Resolve path relative to cwd (without changing global process cwd)."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return str(p.resolve(strict=False))


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


def detect_image_mime_type(path: Path) -> str | None:
    try:
        with path.open("rb") as file:
            header = file.read(16)
    except OSError:
        return None

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = guess_type(path.name)
    if guessed in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return guessed
    return None


def detect_document_mime_type(path: Path) -> str | None:
    try:
        with path.open("rb") as file:
            header = file.read(8)
    except OSError:
        return None

    if header.startswith(b"%PDF-"):
        return "application/pdf"
    guessed, _ = guess_type(path.name)
    if guessed == "application/pdf":
        return guessed
    return None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


# Track active subprocesses for cancellation.
_ACTIVE_PROCS: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCS_LOCK = threading.Lock()


def _kill_proc_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cancel_all_tools() -> None:
    """Terminate all running bash subprocesses."""

    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
        _ACTIVE_PROCS.clear()

    for proc in procs:
        _kill_proc_tree(proc)


ToolOutputCallback = Callable[[str], None]


class ToolExecutor:
    """Execute tool calls for a single session."""

    def __init__(
        self,
        *,
        cwd: str,
        session_dir: Path,
        tools: Sequence[ToolSpec] | None = None,
        supports_image_input: bool = False,
    ):
        self.cwd = str(Path(cwd).resolve(strict=False))
        self.session_dir = session_dir
        self.supports_image_input = supports_image_input
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.tool_output_dir = self.session_dir / "tool-output"
        self.tool_output_dir.mkdir(parents=True, exist_ok=True)
        self._active_procs: set[subprocess.Popen[str]] = set()
        self._active_procs_lock = threading.Lock()
        self.tool_specs = tuple(tools or DEFAULT_TOOL_SPECS)
        self._tools_by_name: dict[str, ToolSpec] = {}

        for spec in self.tool_specs:
            if spec.name in self._tools_by_name:
                raise ValueError(f"duplicate tool name: {spec.name}")
            if not callable(getattr(self, spec.method_name, None)):
                raise ValueError(f"missing tool method: {spec.method_name}")
            self._tools_by_name[spec.name] = spec

    @property
    def definitions(self) -> list[dict[str, Any]]:
        """Return provider-facing tool definitions for the configured tools."""

        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in self.tool_specs
        ]

    def get_tool(self, name: str) -> ToolSpec | None:
        """Return the configured tool spec for a tool name."""

        return self._tools_by_name.get(name)

    def run(self, name: str, *, args: dict[str, Any]) -> ToolExecutionResult:
        """Execute a non-streaming tool call."""

        spec = self.get_tool(name)
        if spec is None:
            return ToolExecutionResult(
                model_text=f"error: unknown tool: {name}",
                display_text=f"Unknown tool: {name}",
                is_error=True,
            )
        if spec.streams_output:
            raise ValueError(f"tool requires streaming execution: {name}")

        handler = cast(Callable[..., ToolExecutionResult], getattr(self, spec.method_name))
        return handler(**args)

    def run_streaming(
        self,
        name: str,
        *,
        tool_call_id: str,
        args: dict[str, Any],
        on_output: ToolOutputCallback,
    ) -> ToolExecutionResult:
        """Execute a tool call that emits incremental output."""

        spec = self.get_tool(name)
        if spec is None:
            return ToolExecutionResult(
                model_text=f"error: unknown tool: {name}",
                display_text=f"Unknown tool: {name}",
                is_error=True,
            )
        if not spec.streams_output:
            raise ValueError(f"tool does not support streaming execution: {name}")

        handler = cast(Callable[..., ToolExecutionResult], getattr(self, spec.method_name))
        return handler(tool_call_id=tool_call_id, on_output=on_output, **args)

    def _track_proc(self, proc: subprocess.Popen[str]) -> None:
        with self._active_procs_lock:
            self._active_procs.add(proc)
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.add(proc)

    def _untrack_proc(self, proc: subprocess.Popen[str]) -> None:
        with self._active_procs_lock:
            self._active_procs.discard(proc)
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.discard(proc)

    def cancel_active(self) -> None:
        """Terminate only bash subprocesses started by this executor."""

        with self._active_procs_lock:
            procs = list(self._active_procs)
            self._active_procs.clear()

        for proc in procs:
            with _ACTIVE_PROCS_LOCK:
                _ACTIVE_PROCS.discard(proc)
            _kill_proc_tree(proc)

    # ---- read -----------------------------------------------------------------

    def read(self, *, path: str, offset: int | None = None, limit: int | None = None) -> ToolExecutionResult:
        """Read a text file or supported image file.

        offset is 1-indexed. limit is number of lines.
        """

        file_path = Path(resolve_path(path, cwd=self.cwd))
        if not file_path.exists():
            return ToolExecutionResult(
                model_text=f"error: file not found: {path}",
                display_text=f"File not found: {path}",
                is_error=True,
            )
        if not file_path.is_file():
            return ToolExecutionResult(
                model_text=f"error: not a file: {path}",
                display_text=f"Not a file: {path}",
                is_error=True,
            )

        image_mime_type = detect_image_mime_type(file_path)
        if image_mime_type:
            if not self.supports_image_input:
                return ToolExecutionResult(
                    model_text="error: image input is not supported by the current model",
                    display_text="Current model does not support image input",
                    is_error=True,
                )
            summary = f"Read image file [{image_mime_type}]"
            image_data = b64encode(file_path.read_bytes()).decode("utf-8")
            return ToolExecutionResult(
                model_text=summary,
                display_text=summary,
                content=[
                    text_block(summary),
                    image_block(image_data, mime_type=image_mime_type, name=file_path.name),
                ],
            )

        start_line = offset if offset and offset > 0 else 1
        line_limit = limit if limit and limit > 0 else DEFAULT_MAX_LINES
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

                    # Keep reads predictable: page by lines and only shorten pathological lines.
                    line = raw_line.rstrip("\r\n")
                    if len(line) > READ_MAX_LINE_CHARS:
                        if first_shortened_line is None:
                            first_shortened_line = total_lines
                        shortened_lines += 1
                        line = line[:READ_MAX_LINE_CHARS] + " ... [line truncated]"
                    lines.append(line)
        except UnicodeDecodeError:
            return ToolExecutionResult(
                model_text=f"error: file is not valid utf-8 text: {path}",
                display_text=f"File is not valid UTF-8 text: {path}",
                is_error=True,
            )
        except Exception as exc:
            return ToolExecutionResult(
                model_text=f"error: failed to read file: {exc}",
                display_text=f"Failed to read file: {path}",
                is_error=True,
            )

        if total_lines < start_line and not (total_lines == 0 and start_line == 1):
            return ToolExecutionResult(
                model_text=f"error: offset {offset} beyond end of file ({total_lines} lines)",
                display_text=f"Offset {offset} beyond end of file: {path}",
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

        content = "\n\n".join(parts) if parts else ""
        return ToolExecutionResult(model_text=content, display_text=content)

    # ---- write ----------------------------------------------------------------

    def write(self, *, path: str, content: str) -> ToolExecutionResult:
        file_path = Path(resolve_path(path, cwd=self.cwd))
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(file_path, content)
        except Exception as exc:
            return ToolExecutionResult(
                model_text=f"error: failed to write file: {exc}",
                display_text=f"Failed to write file: {path}",
                is_error=True,
            )
        return ToolExecutionResult(model_text="ok", display_text=f"Wrote {path}")

    # ---- edit -----------------------------------------------------------------

    def edit(self, *, path: str, edits: list[dict[str, str]]) -> ToolExecutionResult:
        """Replace one or more unique snippets in a file.

        All edits are matched against the original file content (not incrementally).
        Exact match is tried first; if that fails, a conservative fuzzy match
        tolerates line-ending and trailing-whitespace differences while only
        replacing the matched region in the original text.
        """

        file_path = Path(resolve_path(path, cwd=self.cwd))
        if not file_path.exists():
            return ToolExecutionResult(
                model_text=f"error: file not found: {path}",
                display_text=f"File not found: {path}",
                is_error=True,
            )
        if not file_path.is_file():
            return ToolExecutionResult(
                model_text=f"error: not a file: {path}",
                display_text=f"Not a file: {path}",
                is_error=True,
            )
        if not edits:
            return ToolExecutionResult(
                model_text="error: edits must not be empty",
                display_text="Edits list is empty",
                is_error=True,
            )

        multi = len(edits) > 1
        for i, entry in enumerate(edits):
            old_text = entry.get("oldText", "")
            new_text = entry.get("newText", "")
            pfx = f"edits[{i}]: " if multi else ""
            if not old_text:
                return ToolExecutionResult(
                    model_text=f"error: {pfx}oldText must not be empty",
                    display_text="Edit target must not be empty",
                    is_error=True,
                )
            if old_text == new_text:
                return ToolExecutionResult(
                    model_text=f"error: {pfx}oldText and newText are identical",
                    display_text="Edit would not change the file",
                    is_error=True,
                )

        try:
            read_mtime_ns = file_path.stat().st_mtime_ns
            with file_path.open("r", encoding="utf-8", newline="") as file:
                text = file.read()
        except Exception as exc:
            return ToolExecutionResult(
                model_text=f"error: failed to read file: {exc}",
                display_text=f"Failed to read file: {path}",
                is_error=True,
            )

        newline = "\r\n" if "\r\n" in text else None

        # Match all edits against the original text.
        # Each match: (start, end, new_text, edit_index)
        matches: list[tuple[int, int, str, int]] = []
        norm_text: str | None = None
        norm_imap: list[int] | None = None

        for i, entry in enumerate(edits):
            old_text = entry["oldText"]
            new_text = entry["newText"]
            pfx = f"edits[{i}]: " if multi else ""

            exact_count = text.count(old_text)
            if exact_count == 1:
                pos = text.index(old_text)
                matches.append((pos, pos + len(old_text), new_text, i))
                continue
            if exact_count > 1:
                return ToolExecutionResult(
                    model_text=f"error: {pfx}oldText occurs {exact_count} times; provide a more specific oldText",
                    display_text="Edit target is ambiguous",
                    is_error=True,
                )

            # Fuzzy fallback: normalize both sides, find in normalized space,
            # but map the span back to the original text for replacement.
            if norm_text is None:
                norm_text, norm_imap = _normalize_text(text)
            norm_old, _ = _normalize_text(old_text)

            norm_count = norm_text.count(norm_old)
            if norm_count == 0:
                hint = _closest_line_hint(text, old_text)
                msg = f"error: {pfx}oldText not found"
                if hint:
                    msg += f". closest line: {hint}"
                return ToolExecutionResult(
                    model_text=msg,
                    display_text="Edit target not found",
                    is_error=True,
                )
            if norm_count > 1:
                return ToolExecutionResult(
                    model_text=(
                        f"error: {pfx}oldText occurs {norm_count} times after normalization; "
                        "provide a more specific oldText"
                    ),
                    display_text="Edit target is ambiguous after normalization",
                    is_error=True,
                )

            idx = norm_text.find(norm_old)
            assert norm_imap is not None  # set together with norm_text
            orig_start = norm_imap[idx]
            end_idx = idx + len(norm_old)
            orig_end = norm_imap[end_idx] if end_idx < len(norm_imap) else len(text)
            matches.append((orig_start, orig_end, new_text, i))

        # Sort by position and reject overlapping edits.
        matches.sort(key=lambda m: m[0])
        for j in range(1, len(matches)):
            _, prev_end, _, prev_i = matches[j - 1]
            curr_start, _, _, curr_i = matches[j]
            if prev_end > curr_start:
                return ToolExecutionResult(
                    model_text=f"error: edits[{prev_i}] and edits[{curr_i}] overlap",
                    display_text="Edit regions overlap",
                    is_error=True,
                )

        # Apply replacements back-to-front so earlier offsets stay valid.
        updated = text
        for start, end, new_text, _ in reversed(matches):
            updated = updated[:start] + new_text + updated[end:]

        if updated == text:
            return ToolExecutionResult(
                model_text="error: edits produced no changes",
                display_text="Edits would not change the file",
                is_error=True,
            )

        try:
            if file_path.stat().st_mtime_ns != read_mtime_ns:
                return ToolExecutionResult(
                    model_text="error: file changed while editing; read it again and retry",
                    display_text="File changed while editing",
                    is_error=True,
                )
            _atomic_write_text(file_path, updated, newline=newline)
        except Exception as exc:
            return ToolExecutionResult(
                model_text=f"error: failed to write file: {exc}",
                display_text=f"Failed to write file: {path}",
                is_error=True,
            )

        # Build per-edit metadata for the web UI diff view.
        # Matches are sorted by original position; track cumulative character
        # shift so we can compute correct line numbers in the updated text.
        updated_lines = updated.splitlines()
        edit_metas: list[dict[str, Any]] = []
        char_shift = 0
        ctx = 3

        for start, end, new_text, _ in matches:
            old_snippet = text[start:end]
            new_start = start + char_shift
            start_line = updated[:new_start].count("\n") + 1
            old_lc = len(old_snippet.splitlines()) or 1
            new_lc = len(new_text.splitlines()) or 1

            si = start_line - 1
            before = updated_lines[max(0, si - ctx) : si]
            after = updated_lines[si + new_lc : si + new_lc + ctx]

            edit_metas.append(
                {
                    "start_line": start_line,
                    "old_line_count": old_lc,
                    "new_line_count": new_lc,
                    "context_before": before,
                    "context_after": after,
                }
            )
            char_shift += len(new_text) - (end - start)

        n = len(edits)
        display = f"Updated {path}" if n == 1 else f"Updated {path} ({n} edits)"
        return ToolExecutionResult(
            model_text=json.dumps({"status": "ok", "edits": edit_metas}),
            display_text=display,
        )

    # ---- bash -----------------------------------------------------------------

    def bash(
        self,
        *,
        tool_call_id: str,
        command: str,
        timeout: int | None = None,
        on_output: ToolOutputCallback | None = None,
    ) -> ToolExecutionResult:
        """Run a shell command and return combined stdout/stderr text.

        Output is streamed line-by-line through ``on_output`` when provided.

        Truncation has two layers:
        1. Memory protection: when total output exceeds ``_BASH_MAX_IN_MEMORY_BYTES``,
           further output is written to a log file and only a bounded tail
           (``deque(maxlen=DEFAULT_MAX_LINES)``) is kept in memory.
        2. Display truncation: the final text is truncated to
           ``DEFAULT_MAX_LINES`` / ``DEFAULT_MAX_BYTES`` via ``truncate_text``.
        """

        timeout_seconds = int(timeout or BASH_TIMEOUT_SECONDS)
        if timeout_seconds <= 0:
            timeout_seconds = BASH_TIMEOUT_SECONDS

        proc: subprocess.Popen[str] | None = None
        log_path = self.tool_output_dir / f"bash-{tool_call_id}.log"
        # Streaming phase: accumulate in memory until _BASH_MAX_IN_MEMORY_BYTES,
        # then spill to log file and keep only a bounded tail via deque.
        kept_lines: list[str] = []
        kept_bytes = 0
        total_line_count = 0
        tail_lines: deque[str] = deque(maxlen=DEFAULT_MAX_LINES)
        log_file: TextIO | None = None
        saved_output_path: Path | None = None

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                start_new_session=os.name == "posix",
            )
            self._track_proc(proc)

            stdout = cast(TextIO, proc.stdout)
            output_queue: queue.Queue[str | None] = queue.Queue()
            reader_errors: list[Exception] = []

            def read_stdout() -> None:
                try:
                    for line in stdout:
                        output_queue.put(line)
                except Exception as exc:  # pragma: no cover - defensive
                    reader_errors.append(exc)
                finally:
                    output_queue.put(None)

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout_seconds

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_proc_tree(proc)
                    return ToolExecutionResult(
                        model_text=f"error: timeout after {timeout_seconds}s",
                        display_text=f"Command timed out after {timeout_seconds}s",
                        is_error=True,
                    )

                try:
                    line = output_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue

                if line is None:
                    break

                line = line.rstrip("\n")
                total_line_count += 1
                kept_bytes += len((line + "\n").encode("utf-8"))

                if log_file is None:
                    kept_lines.append(line)
                    if kept_bytes > _BASH_MAX_IN_MEMORY_BYTES:
                        log_file = log_path.open("w", encoding="utf-8")
                        saved_output_path = log_path
                        if kept_lines:
                            log_file.write("\n".join(kept_lines))
                            log_file.write("\n")
                            tail_lines.extend(kept_lines)
                        kept_lines = []
                else:
                    tail_lines.append(line)
                    log_file.write(line)
                    log_file.write("\n")

                if on_output:
                    on_output(line)

            if reader_errors:
                message = str(reader_errors[0])
                return ToolExecutionResult(
                    model_text=f"error: {message}",
                    display_text=message,
                    is_error=True,
                )

            try:
                remaining = max(0.1, deadline - time.monotonic())
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill_proc_tree(proc)
                return ToolExecutionResult(
                    model_text=f"error: timeout after {timeout_seconds}s",
                    display_text=f"Command timed out after {timeout_seconds}s",
                    is_error=True,
                )

            exit_code = proc.returncode

            raw_output = "\n".join(list(tail_lines) if log_file is not None else kept_lines)
            output = raw_output.strip() or "(empty)"
            content, trunc = truncate_text(output, tail=True)

            # Save full output to log file when truncated but not already on disk
            if log_file is None and trunc.truncated:
                try:
                    log_path.write_text(raw_output, encoding="utf-8")
                    saved_output_path = log_path
                except Exception:
                    saved_output_path = None

            result = content

            # Truncation notice — either the in-memory tail buffer or the final
            # display truncation removed part of the output.
            shown_lines = trunc.output_lines
            was_truncated = log_file is not None or trunc.truncated
            if was_truncated:
                if trunc.truncated_by == "bytes":
                    if total_line_count <= 1:
                        notice = (
                            f"[Truncated: showing last {DEFAULT_MAX_BYTES // 1024}KB of output "
                            f"({DEFAULT_MAX_BYTES // 1024}KB limit)."
                        )
                    else:
                        notice = f"[Truncated: showing tail output ({DEFAULT_MAX_BYTES // 1024}KB limit)."
                else:
                    notice = f"[Truncated: last {shown_lines} of {total_line_count} lines."
                if saved_output_path is not None:
                    notice += f" Full output: {saved_output_path}]"
                else:
                    notice += "]"
                result += "\n\n" + notice

            if exit_code:
                result += f"\n\n[exit code: {exit_code}]"

            return ToolExecutionResult(model_text=result, display_text=result)

        except Exception as exc:
            message = str(exc)
            return ToolExecutionResult(
                model_text=f"error: {message}",
                display_text=message,
                is_error=True,
            )
        finally:
            if log_file is not None:
                try:
                    log_file.close()
                except Exception:
                    pass
            if proc:
                self._untrack_proc(proc)
                if proc.poll() is None:
                    _kill_proc_tree(proc)


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

    if best_ratio < 0.6 or not best_line:
        return None

    if len(best_line) > 120:
        return best_line[:117] + "..."
    return best_line


def _normalize_text(text: str) -> tuple[str, list[int]]:
    """Normalize for fuzzy edit matching: strip trailing whitespace per line, CRLF→LF.

    Returns (normalized, index_map) where ``index_map[i]`` is the position of
    normalized char *i* in the original text.  This lets callers find a match in
    the normalized string and map the span back to exact original byte offsets,
    so untouched regions of the file are never altered.
    """

    chars: list[str] = []
    imap: list[int] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        trimmed = content.rstrip(" \t")
        for j in range(len(trimmed)):
            chars.append(trimmed[j])
            imap.append(pos + j)
        eol = line[len(content) :]
        if eol:
            chars.append("\n")
            imap.append(pos + len(content))
        pos += len(line)
    return "".join(chars), imap
