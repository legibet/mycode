"""Tool execution runtime.

Runtime exposes four built-in tools: ``read``, ``write``, ``edit``, ``bash``.
External callers register custom tools in two ways:

- Build a :class:`ToolSpec` directly (full control over JSON schema).
- Use :func:`tool` to wrap a plain Python function; the schema is inferred
  from type hints.

Built-in and custom tools share one execution path: ``ToolExecutor.execute``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
import typing
from base64 import b64encode
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Literal, TextIO, cast, get_args, get_origin, overload

from mycode.messages import image_block, text_block

# ---------------------------------------------------------------------------
# Limits (keep token usage low)
# ---------------------------------------------------------------------------
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
READ_MAX_LINE_CHARS = 2000

BASH_TIMEOUT_SECONDS = 120
_BASH_MAX_IN_MEMORY_BYTES = 5_000_000


ToolOutputCallback = Callable[[str], None]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Structured tool result used by the runtime.

    ``model_text`` is appended to session history for future provider replay.
    ``display_text`` is shown to the user.
    """

    model_text: str
    display_text: str
    is_error: bool = False
    content: list[dict[str, Any]] | None = None


ToolRunner = Callable[["ToolContext", dict[str, Any]], ToolExecutionResult]


@dataclass(frozen=True)
class ToolSpec:
    """One tool the agent can call.

    ``runner`` receives a :class:`ToolContext` and the raw argument dict from
    the model. Tools that emit incremental output (currently only ``bash``)
    set ``streams_output=True`` and write lines via ``ctx.emit``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    runner: ToolRunner
    streams_output: bool = False


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
    total_bytes = len(text.encode("utf-8"))
    out_lines: list[str] = []
    out_bytes = 0

    source = reversed(lines) if tail else lines

    for line in source:
        if len(out_lines) >= max_lines:
            break
        b = len(line.encode("utf-8")) + 1  # +1 for newline
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
            output_bytes=len(sliced),
        )

    content = "\n".join(out_lines)
    truncated = len(out_lines) < len(lines) or out_bytes < total_bytes

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
        output_bytes=out_bytes,
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
            if file.read(5).startswith(b"%PDF-"):
                return "application/pdf"
    except OSError:
        pass
    guessed, _ = guess_type(path.name)
    return "application/pdf" if guessed == "application/pdf" else None


# ---------------------------------------------------------------------------
# Subprocess tracking for cancellation
# ---------------------------------------------------------------------------


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
    """Terminate all running bash subprocesses across every executor."""

    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
        _ACTIVE_PROCS.clear()

    for proc in procs:
        _kill_proc_tree(proc)


# ---------------------------------------------------------------------------
# Tool execution context
# ---------------------------------------------------------------------------


class ToolContext:
    """Runtime context passed to a tool's ``runner``.

    Exposes executor configuration, the executor itself (so custom tools can
    invoke other registered tools via :meth:`call`), and streaming helpers for
    tools declared with ``streams_output=True``.
    """

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        tool_call_id: str | None = None,
        emit: ToolOutputCallback | None = None,
    ):
        self.executor = executor
        self.tool_call_id = tool_call_id
        self.emit = emit

    @property
    def cwd(self) -> str:
        return self.executor.cwd

    @property
    def session_dir(self) -> Path:
        return self.executor.session_dir

    @property
    def tool_output_dir(self) -> Path:
        return self.executor.tool_output_dir

    @property
    def supports_image_input(self) -> bool:
        return self.executor.supports_image_input

    def call(self, name: str, args: dict[str, Any]) -> ToolExecutionResult:
        """Invoke another registered tool from inside this one.

        ``tool_call_id`` and ``emit`` from the current context are forwarded,
        so a streaming tool that delegates to ``bash`` keeps producing
        ``tool_output`` events upstream.
        """

        return self.executor.execute(
            name,
            args,
            tool_call_id=self.tool_call_id,
            on_output=self.emit,
        )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


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

        specs = tuple(tools if tools is not None else DEFAULT_TOOL_SPECS)
        self.tool_specs: tuple[ToolSpec, ...] = specs
        self._tools_by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._tools_by_name:
                raise ValueError(f"duplicate tool name: {spec.name}")
            self._tools_by_name[spec.name] = spec

    @property
    def definitions(self) -> list[dict[str, Any]]:
        """Return provider-facing tool definitions."""

        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in self.tool_specs
        ]

    def get(self, name: str) -> ToolSpec | None:
        """Return the registered spec for a tool name."""

        return self._tools_by_name.get(name)

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str | None = None,
        on_output: ToolOutputCallback | None = None,
    ) -> ToolExecutionResult:
        """Execute one registered tool by name.

        ``on_output`` is forwarded to the runner as ``ctx.emit`` for tools that
        stream incremental output.
        """

        spec = self._tools_by_name.get(name)
        if spec is None:
            return ToolExecutionResult(
                model_text=f"error: unknown tool: {name}",
                display_text=f"Unknown tool: {name}",
                is_error=True,
            )
        ctx = ToolContext(executor=self, tool_call_id=tool_call_id, emit=on_output)
        return spec.runner(ctx, args)

    def cancel_active(self) -> None:
        """Terminate bash subprocesses started by this executor."""

        with self._active_procs_lock:
            procs = list(self._active_procs)
            self._active_procs.clear()

        for proc in procs:
            with _ACTIVE_PROCS_LOCK:
                _ACTIVE_PROCS.discard(proc)
            _kill_proc_tree(proc)

    def track_proc(self, proc: subprocess.Popen[str]) -> None:
        """Register a subprocess so ``cancel_active`` and ``cancel_all_tools``
        can terminate it if the agent turn is cancelled."""

        with self._active_procs_lock:
            self._active_procs.add(proc)
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.add(proc)

    def untrack_proc(self, proc: subprocess.Popen[str]) -> None:
        """Remove a subprocess from the cancellation registry once it exits."""

        with self._active_procs_lock:
            self._active_procs.discard(proc)
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.discard(proc)


# ---------------------------------------------------------------------------
# Built-in tool runners
# ---------------------------------------------------------------------------


def _run_read(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    """Read a text file or supported image file.

    ``offset`` is 1-indexed. ``limit`` is the number of lines.
    """

    path = str(args.get("path") or "")
    offset = args.get("offset")
    limit = args.get("limit")
    file_path = Path(resolve_path(path, cwd=ctx.cwd))

    image_mime_type = detect_image_mime_type(file_path)
    if image_mime_type:
        if not ctx.supports_image_input:
            return ToolExecutionResult(
                model_text="error: image input is not supported by the current model",
                display_text="Current model does not support image input",
                is_error=True,
            )
        summary = f"Read image file [{image_mime_type}]"
        try:
            image_data = b64encode(file_path.read_bytes()).decode("utf-8")
        except FileNotFoundError:
            return ToolExecutionResult(
                model_text=f"error: file not found: {path}",
                display_text=f"File not found: {path}",
                is_error=True,
            )
        except Exception as exc:
            return ToolExecutionResult(
                model_text=f"error: failed to read file: {exc}",
                display_text=f"Failed to read file: {path}",
                is_error=True,
            )
        return ToolExecutionResult(
            model_text=summary,
            display_text=summary,
            content=[
                text_block(summary),
                image_block(image_data, mime_type=image_mime_type, name=file_path.name),
            ],
        )

    start_line = offset if isinstance(offset, int) and offset > 0 else 1
    line_limit = limit if isinstance(limit, int) and limit > 0 else DEFAULT_MAX_LINES
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
        return ToolExecutionResult(
            model_text=f"error: file not found: {path}",
            display_text=f"File not found: {path}",
            is_error=True,
        )
    except IsADirectoryError:
        return ToolExecutionResult(
            model_text=f"error: not a file: {path}",
            display_text=f"Not a file: {path}",
            is_error=True,
        )
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

    joined = "\n\n".join(parts) if parts else ""
    return ToolExecutionResult(model_text=joined, display_text=joined)


def _run_write(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    path = str(args.get("path") or "")
    content = str(args.get("content") or "")
    file_path = Path(resolve_path(path, cwd=ctx.cwd))
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


def _run_edit(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    """Replace one or more unique snippets in a file.

    All edits are matched against the original file content (not
    incrementally). Exact match is tried first; if that fails, a conservative
    fuzzy match tolerates line-ending and trailing-whitespace differences
    while only replacing the matched region in the original text.
    """

    path = str(args.get("path") or "")
    raw_edits = args.get("edits")
    if not isinstance(raw_edits, list):
        return ToolExecutionResult(
            model_text="error: edits must be a list",
            display_text="Edits must be a list",
            is_error=True,
        )
    edits = cast(list[dict[str, str]], raw_edits)
    file_path = Path(resolve_path(path, cwd=ctx.cwd))
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
    except FileNotFoundError:
        return ToolExecutionResult(
            model_text=f"error: file not found: {path}",
            display_text=f"File not found: {path}",
            is_error=True,
        )
    except IsADirectoryError:
        return ToolExecutionResult(
            model_text=f"error: not a file: {path}",
            display_text=f"Not a file: {path}",
            is_error=True,
        )
    except Exception as exc:
        return ToolExecutionResult(
            model_text=f"error: failed to read file: {exc}",
            display_text=f"Failed to read file: {path}",
            is_error=True,
        )

    newline = "\r\n" if "\r\n" in text else None

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
    context_lines = 3

    for start, end, new_text, _ in matches:
        old_snippet = text[start:end]
        new_start = start + char_shift
        start_line = updated[:new_start].count("\n") + 1
        old_lc = len(old_snippet.splitlines()) or 1
        new_lc = len(new_text.splitlines()) or 1

        si = start_line - 1
        before = updated_lines[max(0, si - context_lines) : si]
        after = updated_lines[si + new_lc : si + new_lc + context_lines]

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


def _run_bash(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    """Run a shell command and return combined stdout/stderr text.

    Output is streamed line-by-line through ``ctx.emit`` when present.

    Truncation has two layers:
    1. Memory protection: when total output exceeds ``_BASH_MAX_IN_MEMORY_BYTES``,
       further output is written to a log file and only a bounded tail
       (``deque(maxlen=DEFAULT_MAX_LINES)``) is kept in memory.
    2. Display truncation: the final text is truncated to
       ``DEFAULT_MAX_LINES`` / ``DEFAULT_MAX_BYTES`` via ``truncate_text``.
    """

    command = str(args.get("command") or "")
    timeout = args.get("timeout")

    timeout_seconds = int(timeout) if isinstance(timeout, int) and timeout > 0 else BASH_TIMEOUT_SECONDS

    proc: subprocess.Popen[str] | None = None
    log_path = ctx.tool_output_dir / f"bash-{ctx.tool_call_id or 'call'}.log"
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
            cwd=ctx.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            start_new_session=os.name == "posix",
        )
        ctx.executor.track_proc(proc)

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
            kept_bytes += len(line.encode("utf-8")) + 1

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

            if ctx.emit is not None:
                ctx.emit(line)

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

        # Append truncation notice if any output was dropped.
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
        if proc is not None:
            ctx.executor.untrack_proc(proc)
            if proc.poll() is None:
                _kill_proc_tree(proc)


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
        runner=_run_read,
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
        runner=_run_write,
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
        runner=_run_edit,
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
        runner=_run_bash,
        streams_output=True,
    ),
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
    """Normalize for fuzzy edit matching: strip trailing whitespace per line, CRLF→LF.

    Returns (normalized, index_map) where ``index_map[i]`` is the position of
    normalized char *i* in the original text. This lets callers find a match
    in the normalized string and map the span back to exact original byte
    offsets, so untouched regions of the file are never altered.
    """

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


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


@overload
def tool(
    function: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    streams_output: bool = False,
) -> ToolSpec: ...


@overload
def tool(
    function: None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    streams_output: bool = False,
) -> Callable[[Callable[..., Any]], ToolSpec]: ...


def tool(
    function: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    streams_output: bool = False,
) -> ToolSpec | Callable[[Callable[..., Any]], ToolSpec]:
    """Wrap a plain Python function as a :class:`ToolSpec`.

    Sync and async functions are both supported. If the first parameter is
    annotated :class:`ToolContext` the context is injected automatically and
    the remaining parameters drive the JSON schema sent to the provider.

    The function may return a :class:`ToolExecutionResult` or any
    JSON-serializable value; non-result values are wrapped as plain text.
    """

    def wrap(fn: Callable[..., Any]) -> ToolSpec:
        parameters = list(inspect.signature(fn).parameters.values())
        try:
            resolved_hints = typing.get_type_hints(fn)
        except Exception:
            resolved_hints = {}
        wants_context = bool(parameters) and resolved_hints.get(parameters[0].name) is ToolContext
        tool_params = parameters[1:] if wants_context else parameters
        param_names = {p.name for p in tool_params}
        input_schema, coercions = _build_input_schema(tool_params, resolved_hints)

        resolved_description = description or inspect.getdoc(fn)
        if not resolved_description:
            raise ValueError(f"tool {(name or fn.__name__)!r} requires a docstring or explicit description")

        is_async = inspect.iscoroutinefunction(fn)

        def runner(context: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
            call_args: dict[str, Any] = {}
            for key, raw_value in args.items():
                if key not in param_names:
                    continue
                coerce = coercions.get(key)
                call_args[key] = coerce(raw_value) if (coerce is not None and raw_value is not None) else raw_value
            if is_async:
                # The executor itself runs on a worker thread (see Agent loop),
                # so spinning a fresh event loop here is safe.
                value = asyncio.run(fn(context, **call_args) if wants_context else fn(**call_args))
            else:
                value = fn(context, **call_args) if wants_context else fn(**call_args)
            return _coerce_tool_result(value)

        return ToolSpec(
            name=name or fn.__name__,
            description=resolved_description,
            input_schema=input_schema,
            runner=runner,
            streams_output=streams_output,
        )

    if function is None:
        return wrap
    return wrap(function)


def _build_input_schema(
    parameters: list[inspect.Parameter],
    resolved_hints: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Callable[[Any], Any]]]:
    """Build the JSON schema for ``parameters`` and a per-name coercion map.

    The coercion map carries the post-JSON conversions needed when an
    annotation has no native JSON type (currently only ``Path``).
    """

    properties: dict[str, Any] = {}
    required: list[str] = []
    coercions: dict[str, Callable[[Any], Any]] = {}

    for parameter in parameters:
        if parameter.kind not in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}:
            raise ValueError(f"unsupported tool parameter kind: {parameter.name}")

        annotation = resolved_hints.get(parameter.name, parameter.annotation)
        properties[parameter.name] = _annotation_to_schema(annotation, parameter.name)
        coerce = _coercion_for_annotation(annotation)
        if coerce is not None:
            coercions[parameter.name] = coerce
        if parameter.default is inspect.Signature.empty:
            required.append(parameter.name)
            continue

        try:
            json.dumps(parameter.default)
        except TypeError:
            continue
        properties[parameter.name]["default"] = parameter.default

    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    return schema, coercions


def _coercion_for_annotation(annotation: Any) -> Callable[[Any], Any] | None:
    """Return a value coercion to apply after JSON parsing, or None.

    Path is the only non-JSON-native type that ``_annotation_to_schema``
    accepts; the runner must rebuild a Path from the raw string the model
    sends.
    """

    if annotation is Path:
        return Path

    args = get_args(annotation)
    if args and type(None) in args:
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            return _coercion_for_annotation(non_none[0])

    return None


def _annotation_to_schema(annotation: Any, parameter_name: str) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        raise TypeError(f"tool parameter {parameter_name!r} requires a type annotation")
    if annotation in {str, Path}:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in {list, tuple, set}:
        items = _annotation_to_schema(args[0], parameter_name) if args else {"type": "string"}
        return {"type": "array", "items": items}

    if origin is Literal:
        values = list(args)
        if not values:
            raise TypeError(f"Literal annotation on {parameter_name!r} must list at least one value")
        schema = _annotation_to_schema(type(values[0]), parameter_name)
        schema["enum"] = values
        return schema

    if args and type(None) in args:
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            return _annotation_to_schema(non_none[0], parameter_name)

    raise TypeError(f"unsupported tool parameter type for {parameter_name!r}: {annotation!r}")


def _coerce_tool_result(value: Any) -> ToolExecutionResult:
    if isinstance(value, ToolExecutionResult):
        return value
    if isinstance(value, str):
        return ToolExecutionResult(model_text=value, display_text=value)
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError:
        text = str(value)
    return ToolExecutionResult(model_text=text, display_text=text)
