"""Core tool definitions and execution.

This module intentionally exposes only **four** tools:
- read
- write
- edit
- bash

Tool schemas are passed to the LLM as native Messages API tools.
Execution is implemented in :class:`ToolExecutor`.

Design goals (inspired by pi):
- minimal primitives
- predictable truncation
- actionable continuation hints
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TextIO, cast

# ---------------------------------------------------------------------------
# Limits (keep token usage low)
# ---------------------------------------------------------------------------

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024

BASH_TIMEOUT_SECONDS = 120
_BASH_MAX_IN_MEMORY_BYTES = 5_000_000


# ---------------------------------------------------------------------------
# Tool schemas (Messages API)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read",
        "description": "Read a file. Text output is truncated to 2000 lines or 50KB. Use offset/limit for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "offset": {"type": "integer", "description": "Line number to start from (1-indexed)."},
                "limit": {"type": "integer", "description": "Maximum number of lines to return."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write",
        "description": "Write a file (create or overwrite).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "content": {"type": "string", "description": "File content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit",
        "description": "Edit a file by replacing an exact oldText snippet with newText. oldText must match exactly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative or absolute)."},
                "oldText": {"type": "string", "description": "Exact text to replace (must match exactly)."},
                "newText": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "oldText", "newText"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash",
        "description": (
            "Run a shell command in the session working directory. "
            "Output is truncated; if truncated, the full output is written to a file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (optional)."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


@dataclass(frozen=True)
class Truncation:
    truncated: bool
    truncated_by: str | None
    output_lines: int
    output_bytes: int


def truncate_text(
    text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[str, Truncation]:
    """Truncate text by both line and byte limits.

    Returns (content, truncation).
    """

    lines = text.splitlines()
    out_lines: list[str] = []
    out_bytes = 0

    for line in lines[:max_lines]:
        # +1 for newline when joined later
        b = len((line + "\n").encode("utf-8"))
        if out_bytes + b > max_bytes:
            break
        out_lines.append(line)
        out_bytes += b

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


def parse_tool_arguments(raw: str | None) -> dict[str, Any] | str:
    """Parse a JSON tool-arguments payload.

    Returns either the parsed object or an error string. Keeping this helper here
    makes tool-argument validation consistent across adapters and tests.
    """

    if raw is None:
        return {}

    payload = raw.strip()
    if not payload:
        return {}

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return "error: invalid JSON arguments"

    if not isinstance(parsed, dict):
        return "error: tool arguments must decode to a JSON object"

    return parsed


def resolve_path(path: str, *, cwd: str) -> str:
    """Resolve path relative to cwd (without changing global process cwd)."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return str(p.resolve(strict=False))


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _full_output_note(path: Path | None, *, in_memory: bool) -> str:
    note = "\n\n[Output truncated in memory.]" if in_memory else "\n\n[Output truncated.]"
    if path is not None:
        note += f" Full output saved to: {path} (use read)."
    return note


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


# Track active subprocesses for cancellation.
_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_PROCS_LOCK = threading.Lock()


def _kill_proc_tree(proc: subprocess.Popen[Any]) -> None:
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

    def __init__(self, *, cwd: str, session_dir: Path):
        self.cwd = str(Path(cwd).resolve(strict=False))
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.tool_output_dir = self.session_dir / "tool-output"
        self.tool_output_dir.mkdir(parents=True, exist_ok=True)
        self._active_procs: set[subprocess.Popen[str]] = set()
        self._active_procs_lock = threading.Lock()

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

    def _resolve_existing_file(self, path: str) -> tuple[Path | None, str | None]:
        file_path = Path(resolve_path(path, cwd=self.cwd))
        if not file_path.exists():
            return None, f"error: file not found: {path}"
        if not file_path.is_file():
            return None, f"error: not a file: {path}"
        return file_path, None

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

    def read(self, *, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a text file.

        offset is 1-indexed. limit is number of lines.
        """

        file_path, error = self._resolve_existing_file(path)
        if error:
            return error
        assert file_path is not None

        try:
            raw = file_path.read_bytes()
        except Exception as exc:
            return f"error: failed to read file: {exc}"

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"error: file is not valid utf-8 text: {path}"

        all_lines = text.splitlines()
        total_lines = len(all_lines)

        start = max(0, (offset or 1) - 1)
        if start >= total_lines:
            return f"error: offset {offset} beyond end of file ({total_lines} lines)"

        selected = all_lines[start : start + (limit or total_lines)]
        selected_text = "\n".join(selected)

        content, trunc = truncate_text(selected_text)

        if trunc.truncated:
            shown_from = start + 1
            shown_to = start + trunc.output_lines
            next_offset = shown_to + 1
            note = (
                f"\n\n[Showing lines {shown_from}-{shown_to} of {total_lines} "
                f"({_format_size(DEFAULT_MAX_BYTES)} limit). Use offset={next_offset} to continue.]"
            )
            return content + note

        # User-limited (but not truncated)
        if limit is not None and (start + len(selected)) < total_lines:
            next_offset = start + len(selected) + 1
            remaining = total_lines - (start + len(selected))
            return content + f"\n\n[{remaining} more lines. Use offset={next_offset} to continue.]"

        return content

    # ---- write ----------------------------------------------------------------

    def write(self, *, path: str, content: str) -> str:
        abs_path = resolve_path(path, cwd=self.cwd)
        p = Path(abs_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(p, content)
        except Exception as exc:
            return f"error: failed to write file: {exc}"
        return "ok"

    # ---- edit -----------------------------------------------------------------

    def edit(self, *, path: str, oldText: str, newText: str) -> str:  # noqa: N803 (pi-compatible)
        """Replace one exact snippet, with a narrow fuzzy fallback.

        The fallback only tolerates line-ending and trailing-whitespace changes.
        It still requires a unique match so the edit stays deterministic.
        """

        file_path, error = self._resolve_existing_file(path)
        if error:
            return error
        assert file_path is not None

        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"error: failed to read file: {exc}"

        # Exact match first (deterministic and preferred)
        exact_count = text.count(oldText)
        if exact_count == 1:
            updated = text.replace(oldText, newText, 1)
        elif exact_count > 1:
            return f"error: oldText occurs {exact_count} times; provide a more specific oldText"
        else:
            # Conservative fuzzy fallback:
            # tolerate line-ending and trailing-whitespace differences only.
            fuzzy_span, fuzzy_count = _find_fuzzy_edit_span(text, oldText)
            if fuzzy_span is None:
                if fuzzy_count > 1:
                    return (
                        f"error: oldText occurs {fuzzy_count} times after normalization; "
                        "provide a more specific oldText"
                    )
                hint = _closest_line_hint(text, oldText)
                if hint:
                    return f"error: oldText not found. closest line: {hint}"
                return "error: oldText not found"

            start, end = fuzzy_span
            updated = text[:start] + newText + text[end:]

        try:
            _atomic_write_text(file_path, updated)
        except Exception as exc:
            return f"error: failed to write file: {exc}"

        return "ok"

    # ---- bash -----------------------------------------------------------------

    def bash(
        self,
        *,
        tool_call_id: str,
        command: str,
        timeout: int | None = None,
        on_output: ToolOutputCallback | None = None,
    ) -> str:
        """Run a shell command and return combined stdout/stderr text.

        Output is streamed line-by-line through ``on_output`` when provided. If
        the output grows too large for memory or needs truncation, the full log
        is written under the session's ``tool-output/`` directory.
        """

        timeout = int(timeout or BASH_TIMEOUT_SECONDS)
        if timeout <= 0:
            timeout = BASH_TIMEOUT_SECONDS

        proc: subprocess.Popen[str] | None = None
        log_path = self.tool_output_dir / f"bash-{tool_call_id}.log"
        out_lines: list[str] = []
        out_bytes = 0
        tail_lines: deque[str] = deque(maxlen=DEFAULT_MAX_LINES)
        full_path: Path | None = None
        full_file: TextIO | None = None
        spilled_to_file = False

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

            def _read_stdout() -> None:
                try:
                    for line in stdout:
                        output_queue.put(line)
                except Exception as exc:  # pragma: no cover - defensive
                    reader_errors.append(exc)
                finally:
                    output_queue.put(None)

            reader = threading.Thread(target=_read_stdout, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_proc_tree(proc)
                    return f"error: timeout after {timeout}s"

                try:
                    line = output_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue

                if line is None:
                    break

                line = line.rstrip("\n")
                line_bytes = len((line + "\n").encode("utf-8"))
                out_bytes += line_bytes

                if spilled_to_file:
                    tail_lines.append(line)
                    assert full_file is not None
                    full_file.write(line)
                    full_file.write("\n")
                else:
                    out_lines.append(line)
                    if out_bytes > _BASH_MAX_IN_MEMORY_BYTES:
                        full_path = log_path
                        full_file = full_path.open("w", encoding="utf-8")
                        if out_lines:
                            full_file.write("\n".join(out_lines))
                            full_file.write("\n")
                            tail_lines.extend(out_lines)
                        out_lines = []
                        spilled_to_file = True

                if on_output:
                    on_output(line)

            if reader_errors:
                return f"error: {reader_errors[0]}"

            try:
                remaining = max(0.1, deadline - time.monotonic())
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill_proc_tree(proc)
                return f"error: timeout after {timeout}s"

            output_lines = list(tail_lines) if spilled_to_file else out_lines
            raw_output = "\n".join(output_lines)
            output = raw_output.strip() or "(empty)"
            content, trunc = truncate_text(output)

            if spilled_to_file:
                return content + _full_output_note(full_path, in_memory=True)

            if trunc.truncated:
                # Write full output to session file for later read
                full_path = log_path
                try:
                    full_path.write_text(raw_output, encoding="utf-8")
                except Exception:
                    full_path = None

                return content + _full_output_note(full_path, in_memory=False)

            return content

        except Exception as exc:
            return f"error: {exc}"
        finally:
            if full_file is not None:
                try:
                    full_file.close()
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
