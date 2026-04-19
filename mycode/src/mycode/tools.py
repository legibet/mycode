"""Tool execution runtime.

The SDK ships four built-in coding tools (``read`` / ``write`` / ``edit`` /
``bash``) as :data:`DEFAULT_TOOL_SPECS`. SDK users register additional tools
either by building :class:`ToolSpec` directly or by wrapping a typed function
with :func:`tool`.

Every tool runner receives a :class:`ToolContext` and a raw argument dict.
Context exposes the runtime state (cwd, output directory, image support) plus
typed facades for the four built-ins so custom tools can invoke them without
stringly-typed dispatch::

    @tool
    def smart_read(ctx: ToolContext, path: str) -> str:
        return ctx.read(path).output
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
# Limits
# ---------------------------------------------------------------------------
# read
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
READ_MAX_LINE_CHARS = 2000
# bash
BASH_TIMEOUT_SECONDS = 120
_BASH_MAX_IN_MEMORY_BYTES = 5_000_000


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

ToolOutputCallback = Callable[[str], None]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Result of one tool run.

    Two consumers read this:

    - **provider** — gets ``output`` (a text summary) and, for multimodal
      returns, ``content`` (structured blocks such as an image). ``output``
      is replayed on later turns.
    - **UI** — reads ``metadata`` for structured rendering (e.g. edit diff
      line numbers). When ``metadata`` is absent, the UI falls back to
      ``output`` as a one-line summary.
    """

    output: str
    content: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    is_error: bool = False


ToolRunner = Callable[["ToolContext", dict[str, Any]], ToolExecutionResult]


@dataclass(frozen=True)
class ToolSpec:
    """One tool the agent can call.

    ``runner`` receives a :class:`ToolContext` and the raw argument dict.
    Tools that emit incremental output (``bash``) set ``streams_output=True``
    and push lines via ``ctx.emit``; the agent forwards them as
    ``tool_output`` events live during execution.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    runner: ToolRunner
    streams_output: bool = False


# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call runtime context handed to every tool runner.

    ``cwd`` / ``tool_output_dir`` / ``supports_image_input`` describe where
    the tool runs and what the model accepts. ``tool_call_id`` and ``emit``
    are set by the agent for live streaming; they are ``None`` when a tool
    is invoked outside the agent loop (e.g. TUI attachment read).

    The four facades :meth:`read`, :meth:`write`, :meth:`edit`, :meth:`bash`
    are the SDK-friendly way to invoke the built-in tools from custom code.
    :meth:`call` is a generic fallback for dispatching to any registered
    tool by name.
    """

    executor: ToolExecutor
    cwd: str
    tool_output_dir: Path
    supports_image_input: bool = False
    tool_call_id: str | None = None
    emit: ToolOutputCallback | None = None

    # Typed facades for the built-ins.

    def read(
        self,
        path: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> ToolExecutionResult:
        return self.call("read", {"path": path, "offset": offset, "limit": limit})

    def write(self, path: str, content: str) -> ToolExecutionResult:
        return self.call("write", {"path": path, "content": content})

    def edit(self, path: str, edits: list[dict[str, str]]) -> ToolExecutionResult:
        return self.call("edit", {"path": path, "edits": edits})

    def bash(self, command: str, *, timeout: int | None = None) -> ToolExecutionResult:
        return self.call("bash", {"command": command, "timeout": timeout})

    def call(self, name: str, args: dict[str, Any]) -> ToolExecutionResult:
        """Dispatch to a tool by name. Used by the facades above and by
        custom tools that wrap another registered tool."""

        return self.executor.execute(name, args, self)

    # Subprocess hooks — used by bash to register/unregister its child so
    # ``Agent.cancel`` and ``cancel_all_tools`` can terminate it.

    def track_proc(self, proc: subprocess.Popen[str]) -> None:
        self.executor.track_proc(proc)

    def untrack_proc(self, proc: subprocess.Popen[str]) -> None:
        self.executor.untrack_proc(proc)


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Tool registry plus subprocess lifecycle for one agent session.

    Agents construct one executor internally; SDK users normally pass
    ``tools=[...]`` to :class:`~mycode.agent.Agent` rather than building
    this directly.
    """

    def __init__(self, tools: Sequence[ToolSpec]):
        names = [spec.name for spec in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tool name in specs: {names}")
        self._tools: dict[str, ToolSpec] = {spec.name: spec for spec in tools}
        self._active_procs: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()

    @property
    def definitions(self) -> list[dict[str, Any]]:
        """Provider-facing tool definitions (name / description / schema)."""

        return [
            {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
            for spec in self._tools.values()
        ]

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolExecutionResult(output=f"error: unknown tool: {name}", is_error=True)
        return spec.runner(ctx, args)

    # Subprocess tracking: register with both this executor (per-session
    # cancel) and the process-global set (shutdown cleanup).

    def track_proc(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._active_procs.add(proc)
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.add(proc)

    def untrack_proc(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._active_procs.discard(proc)
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.discard(proc)

    def cancel_active(self) -> None:
        """Terminate bash subprocesses started by this executor."""

        with self._lock:
            procs = list(self._active_procs)
            self._active_procs.clear()
        for proc in procs:
            with _ACTIVE_PROCS_LOCK:
                _ACTIVE_PROCS.discard(proc)
            _kill_proc_tree(proc)


# ---------------------------------------------------------------------------
# Subprocess lifecycle (process-global)
# ---------------------------------------------------------------------------
# Bash registers each child in both the executor and this module-level set.
# The executor set scopes cancellation to one session (multiple agents may
# run concurrently in the same process); the global set is a shutdown-time
# safety net exposed as ``cancel_all_tools``.

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
    """Terminate every running bash subprocess in the current process."""

    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
        _ACTIVE_PROCS.clear()
    for proc in procs:
        _kill_proc_tree(proc)


# ---------------------------------------------------------------------------
# Generic file utilities
# ---------------------------------------------------------------------------


def resolve_path(path: str, *, cwd: str) -> str:
    """Resolve ``path`` relative to ``cwd`` without changing the process cwd."""

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
    """Truncate text by both line and byte limits; keep head or tail."""

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

    # Edge case: a single line exceeds max_bytes — slice the tail/head.
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


# ---------------------------------------------------------------------------
# MIME detection
# ---------------------------------------------------------------------------


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
# Built-in: read
# ---------------------------------------------------------------------------


def _run_read(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    """Read a UTF-8 text file or a supported image file.

    ``offset`` is 1-indexed. ``limit`` caps the number of lines returned.
    """

    path = str(args.get("path") or "")
    offset = args.get("offset")
    limit = args.get("limit")
    file_path = Path(resolve_path(path, cwd=ctx.cwd))

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
# Built-in: write
# ---------------------------------------------------------------------------


def _run_write(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    path = str(args.get("path") or "")
    content = str(args.get("content") or "")
    file_path = Path(resolve_path(path, cwd=ctx.cwd))
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(file_path, content)
    except Exception as exc:
        return ToolExecutionResult(output=f"error: failed to write file: {exc}", is_error=True)
    return ToolExecutionResult(output=f"Wrote {path}")


# ---------------------------------------------------------------------------
# Built-in: edit
# ---------------------------------------------------------------------------


def _run_edit(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    """Replace one or more unique snippets in a file.

    Edits all match against the original file content. Exact match is tried
    first; a conservative fuzzy fallback tolerates line-ending and trailing-
    whitespace differences while only replacing the matched region.
    """

    path = str(args.get("path") or "")
    raw_edits = args.get("edits")
    if not isinstance(raw_edits, list):
        return ToolExecutionResult(output="error: edits must be a list", is_error=True)
    edits = cast(list[dict[str, str]], raw_edits)
    file_path = Path(resolve_path(path, cwd=ctx.cwd))
    if not edits:
        return ToolExecutionResult(output="error: edits must not be empty", is_error=True)

    multi = len(edits) > 1
    for i, entry in enumerate(edits):
        old_text = entry.get("oldText", "")
        new_text = entry.get("newText", "")
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
                output=f"error: {pfx}oldText occurs {exact_count} times; provide a more specific oldText",
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

    # Per-edit metadata for the web diff view. ``matches`` is sorted by
    # original offset; track cumulative char shift to compute correct line
    # numbers in the updated text.
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

    summary = f"Updated {path}" if len(edits) == 1 else f"Updated {path} ({len(edits)} edits)"
    return ToolExecutionResult(output=summary, metadata={"edits": edit_metas})


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
    normalized char *i* in the original text, so callers can map matches in
    the normalized string back to exact original byte offsets.
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
# Built-in: bash
# ---------------------------------------------------------------------------


def _run_bash(ctx: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
    """Run a shell command and return combined stdout/stderr text.

    Output is streamed line-by-line through ``ctx.emit`` when set.

    Truncation has two layers: when total output exceeds the in-memory
    ceiling, further output spills to ``tool_output_dir/bash-<id>.log`` and
    only a bounded tail stays in memory; the returned text is then truncated
    again by :func:`truncate_text` to the display limits.
    """

    command = str(args.get("command") or "")
    timeout = args.get("timeout")

    timeout_seconds = int(timeout) if isinstance(timeout, int) and timeout > 0 else BASH_TIMEOUT_SECONDS

    proc: subprocess.Popen[str] | None = None
    log_path = ctx.tool_output_dir / f"bash-{ctx.tool_call_id or 'call'}.log"
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
        ctx.track_proc(proc)

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
                return ToolExecutionResult(output=f"error: timeout after {timeout_seconds}s", is_error=True)

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
                    # Spill to disk. Create the output dir lazily on first
                    # spill so runs that stay in memory never touch the FS.
                    ctx.tool_output_dir.mkdir(parents=True, exist_ok=True)
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
            return ToolExecutionResult(output=f"error: {reader_errors[0]}", is_error=True)

        try:
            remaining = max(0.1, deadline - time.monotonic())
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            return ToolExecutionResult(output=f"error: timeout after {timeout_seconds}s", is_error=True)

        exit_code = proc.returncode

        raw_output = "\n".join(list(tail_lines) if log_file is not None else kept_lines)
        output = raw_output.strip() or "(empty)"
        content, trunc = truncate_text(output, tail=True)

        # If truncation happened but we never spilled, save the full output
        # now so the user can inspect it via the "Full output" hint.
        if log_file is None and trunc.truncated:
            try:
                ctx.tool_output_dir.mkdir(parents=True, exist_ok=True)
                log_path.write_text(raw_output, encoding="utf-8")
                saved_output_path = log_path
            except Exception:
                saved_output_path = None

        result = content
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

        return ToolExecutionResult(output=result)

    except Exception as exc:
        return ToolExecutionResult(output=f"error: {exc}", is_error=True)
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        if proc is not None:
            ctx.untrack_proc(proc)
            if proc.poll() is None:
                _kill_proc_tree(proc)


# ---------------------------------------------------------------------------
# Built-in tool specs
# ---------------------------------------------------------------------------

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
    annotated :class:`ToolContext`, the context is injected automatically
    and the remaining parameters drive the JSON schema sent to the provider.

    The function may return a :class:`ToolExecutionResult` or any
    JSON-serializable value; non-result values are wrapped as text output.
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
                # The executor runs on a worker thread, so spinning a fresh
                # event loop here is safe.
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

    The coercion map carries post-JSON conversions for annotations with no
    native JSON type (currently only ``Path``).
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


def _coercion_for_annotation(annotation: Any) -> Callable[[Any], Any] | None:
    """Return a value coercion to apply after JSON parsing, or None."""

    if annotation is Path:
        return Path

    args = get_args(annotation)
    if args and type(None) in args:
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            return _coercion_for_annotation(non_none[0])

    return None


def _coerce_tool_result(value: Any) -> ToolExecutionResult:
    if isinstance(value, ToolExecutionResult):
        return value
    if isinstance(value, str):
        return ToolExecutionResult(output=value)
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError:
        text = str(value)
    return ToolExecutionResult(output=text)
