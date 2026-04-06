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
        description="Edit a file by replacing one oldText snippet with newText. Prefer an exact match.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "oldText": {"type": "string", "description": "Exact text to replace (must match exactly)."},
                "newText": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "oldText", "newText"],
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
                "Use bash to inspect it in bytes:\n"
                f"sed -n '{first_shortened_line}p' {quoted} | head -c 2000\n"
                f"sed -n '{first_shortened_line}p' {quoted} | tail -c +2001 | head -c 2000]"
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

    def edit(self, *, path: str, oldText: str, newText: str) -> ToolExecutionResult:  # noqa: N803 (pi-compatible)
        """Replace one unique snippet in a file.

        Tries exact match first. If that fails, only line-ending and trailing-
        whitespace differences are tolerated.
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
        if oldText == "":
            return ToolExecutionResult(
                model_text="error: oldText must not be empty",
                display_text="Edit target must not be empty",
                is_error=True,
            )
        if oldText == newText:
            return ToolExecutionResult(
                model_text="error: oldText and newText are identical",
                display_text="Edit would not change the file",
                is_error=True,
            )

        try:
            # newline="" keeps the original line endings in memory.
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

        # Exact match first (deterministic and preferred)
        exact_count = text.count(oldText)
        match_pos: int | None = None
        if exact_count == 1:
            match_pos = text.index(oldText)
            updated = text.replace(oldText, newText, 1)
        elif exact_count > 1:
            return ToolExecutionResult(
                model_text=f"error: oldText occurs {exact_count} times; provide a more specific oldText",
                display_text="Edit target is ambiguous; provide a more specific oldText",
                is_error=True,
            )
        else:
            # Conservative fuzzy fallback:
            # tolerate line-ending and trailing-whitespace differences only.
            fuzzy_span, fuzzy_count = _find_fuzzy_edit_span(text, oldText)
            if fuzzy_span is None:
                if fuzzy_count > 1:
                    return ToolExecutionResult(
                        model_text=(
                            f"error: oldText occurs {fuzzy_count} times after normalization; "
                            "provide a more specific oldText"
                        ),
                        display_text="Edit target is ambiguous after normalization",
                        is_error=True,
                    )
                hint = _closest_line_hint(text, oldText)
                if hint:
                    return ToolExecutionResult(
                        model_text=f"error: oldText not found. closest line: {hint}",
                        display_text="Edit target not found",
                        is_error=True,
                    )
                return ToolExecutionResult(
                    model_text="error: oldText not found",
                    display_text="Edit target not found",
                    is_error=True,
                )

            start, end = fuzzy_span
            match_pos = start
            updated = text[:start] + newText + text[end:]

        try:
            # Avoid overwriting a file that changed after we read it.
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

        # Return a compact JSON payload so the web UI can render a focused diff
        # around the edited range without re-reading the whole file.
        start_line = text[:match_pos].count("\n") + 1
        old_line_count = len(oldText.splitlines()) or 1
        new_line_count = len(newText.splitlines()) or 1
        context_lines = 3
        lines = updated.splitlines()
        edit_start = start_line - 1
        before = lines[max(0, edit_start - context_lines) : edit_start]
        after = lines[edit_start + new_line_count : edit_start + new_line_count + context_lines]

        return ToolExecutionResult(
            model_text=json.dumps(
                {
                    "status": "ok",
                    "start_line": start_line,
                    "old_line_count": old_line_count,
                    "new_line_count": new_line_count,
                    "context_before": before,
                    "context_after": after,
                }
            ),
            display_text=f"Updated {path}",
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

        Output is streamed line-by-line through ``on_output`` when provided. If
        the output grows too large for memory or needs truncation, the full log
        is written under the session's ``tool-output/`` directory.
        """

        timeout_seconds = int(timeout or BASH_TIMEOUT_SECONDS)
        if timeout_seconds <= 0:
            timeout_seconds = BASH_TIMEOUT_SECONDS

        proc: subprocess.Popen[str] | None = None
        log_path = self.tool_output_dir / f"bash-{tool_call_id}.log"
        kept_lines: list[str] = []
        kept_bytes = 0
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

            raw_output = "\n".join(list(tail_lines) if log_file is not None else kept_lines)
            output = raw_output.strip() or "(empty)"
            content, trunc = truncate_text(output, tail=True)

            if log_file is None and trunc.truncated:
                try:
                    log_path.write_text(raw_output, encoding="utf-8")
                    saved_output_path = log_path
                except Exception:
                    saved_output_path = None

            if log_file is not None or trunc.truncated:
                result = content
                if log_file is not None:
                    result += "\n\n[Output truncated in memory.]"
                else:
                    result += "\n\n[Output truncated.]"

                if saved_output_path is not None:
                    result += f" Full output saved to: {saved_output_path}. Use read with offset/limit."
                    if not content:
                        quoted = shlex.quote(str(saved_output_path))
                        result += (
                            "\nUse bash to inspect bytes:\n"
                            f"head -c 2000 {quoted}\n"
                            f"tail -c +2001 {quoted} | head -c 2000"
                        )
                return ToolExecutionResult(model_text=result, display_text=result)

            return ToolExecutionResult(model_text=content, display_text=content)

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


def _find_fuzzy_edit_span(text: str, old_text: str) -> tuple[tuple[int, int] | None, int]:
    """Find unique match span with conservative normalization.

    Normalization is intentionally limited to:
    - line ending normalization (CRLF/CR -> LF)
    - trailing space/tab removal per line
    """

    normalized_text, text_map = _normalize_for_fuzzy_edit(text)
    normalized_old, _ = _normalize_for_fuzzy_edit(old_text)

    first = normalized_text.find(normalized_old)
    if first == -1:
        return None, 0

    count = normalized_text.count(normalized_old)
    if count != 1:
        return None, count

    end_normalized = first + len(normalized_old)
    start_original = text_map[first]
    end_original = text_map[end_normalized] if end_normalized < len(text_map) else len(text)
    return (start_original, end_original), 1


def _normalize_for_fuzzy_edit(text: str) -> tuple[str, list[int]]:
    """Normalize text for conservative fuzzy edit matching.

    Returns normalized text plus a map from normalized index -> original index.
    """

    chars: list[str] = []
    index_map: list[int] = []

    i = 0
    n = len(text)
    while i < n:
        line_start = i
        while i < n and text[i] not in ("\n", "\r"):
            i += 1

        line_end = i
        trimmed_end = line_end
        while trimmed_end > line_start and text[trimmed_end - 1] in (" ", "\t"):
            trimmed_end -= 1

        for pos in range(line_start, trimmed_end):
            chars.append(text[pos])
            index_map.append(pos)

        if i >= n:
            continue

        # Normalize any line ending to LF and map it to the original EOL start index.
        eol_start = i
        if text[i] == "\r" and i + 1 < n and text[i + 1] == "\n":
            i += 2
        else:
            i += 1
        chars.append("\n")
        index_map.append(eol_start)

    return "".join(chars), index_map
