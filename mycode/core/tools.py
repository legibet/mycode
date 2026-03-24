"""Core tool definitions and execution.

This module intentionally ships with only four built-in tools: `read`,
`write`, `edit`, and `bash`.

`ToolSpec` is the internal source of truth for built-in tool metadata.
`ToolExecutor` owns both execution and the provider-facing tool definitions used
by the agent loop.
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
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TextIO, cast

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


DEFAULT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read",
        description=(
            "Read a UTF-8 text file. Returns up to 2000 lines. "
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
        description="Edit a file by replacing an exact oldText snippet with newText. oldText must match exactly.",
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

    def __init__(self, *, cwd: str, session_dir: Path, tools: Sequence[ToolSpec] | None = None):
        self.cwd = str(Path(cwd).resolve(strict=False))
        self.session_dir = session_dir
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

    def run(self, name: str, *, args: dict[str, Any]) -> str:
        """Execute a non-streaming tool call."""

        spec = self.get_tool(name)
        if spec is None:
            return f"error: unknown tool: {name}"
        if spec.streams_output:
            raise ValueError(f"tool requires streaming execution: {name}")

        handler = cast(Callable[..., str], getattr(self, spec.method_name))
        return handler(**args)

    def run_streaming(
        self,
        name: str,
        *,
        tool_call_id: str,
        args: dict[str, Any],
        on_output: ToolOutputCallback,
    ) -> str:
        """Execute a tool call that emits incremental output."""

        spec = self.get_tool(name)
        if spec is None:
            return f"error: unknown tool: {name}"
        if not spec.streams_output:
            raise ValueError(f"tool does not support streaming execution: {name}")

        handler = cast(Callable[..., str], getattr(self, spec.method_name))
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
            return f"error: file is not valid utf-8 text: {path}"
        except Exception as exc:
            return f"error: failed to read file: {exc}"

        if total_lines < start_line and not (total_lines == 0 and start_line == 1):
            return f"error: offset {offset} beyond end of file ({total_lines} lines)"

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

        if not parts:
            return ""
        return "\n\n".join(parts)

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
        match_pos: int | None = None
        if exact_count == 1:
            match_pos = text.index(oldText)
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
            match_pos = start
            updated = text[:start] + newText + text[end:]

        try:
            _atomic_write_text(file_path, updated)
        except Exception as exc:
            return f"error: failed to write file: {exc}"

        return self._edit_result(text, updated, oldText, newText, match_pos=match_pos)

    @staticmethod
    def _edit_result(
        original: str,
        updated: str,
        old_text: str,
        new_text: str,
        *,
        match_pos: int | None = None,
    ) -> str:
        """Build a JSON result with line context for the frontend diff view."""
        import json

        _CTX = 3
        if match_pos is None:
            match_pos = original.index(old_text)
        start_line = original[:match_pos].count("\n") + 1  # 1-indexed

        old_lc = len(old_text.splitlines()) or 1
        new_lc = len(new_text.splitlines()) or 1

        lines = updated.splitlines()
        edit_start = start_line - 1  # 0-indexed
        before = lines[max(0, edit_start - _CTX) : edit_start]
        after = lines[edit_start + new_lc : edit_start + new_lc + _CTX]

        return json.dumps(
            {
                "status": "ok",
                "start_line": start_line,
                "old_line_count": old_lc,
                "new_line_count": new_lc,
                "context_before": before,
                "context_after": after,
            }
        )

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
            content, trunc = truncate_text(output, tail=True)

            if not spilled_to_file and trunc.truncated:
                full_path = log_path
                try:
                    full_path.write_text(raw_output, encoding="utf-8")
                except Exception:
                    full_path = None

            if spilled_to_file or trunc.truncated:
                result = content
                result += "\n\n[Output truncated in memory.]" if spilled_to_file else "\n\n[Output truncated.]"
                if full_path is not None:
                    result += f" Full output saved to: {full_path}. Use read with offset/limit."
                    if not content:
                        quoted = shlex.quote(str(full_path))
                        result += (
                            "\nUse bash to inspect bytes:\n"
                            f"head -c 2000 {quoted}\n"
                            f"tail -c +2001 {quoted} | head -c 2000"
                        )
                return result

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
