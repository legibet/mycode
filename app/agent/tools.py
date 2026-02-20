"""Core tool definitions and execution.

This module intentionally exposes only **four** tools:
- read
- write
- edit
- bash

Tool schemas are passed to the LLM (OpenAI-style function tools).
Execution is implemented in :class:`ToolExecutor`.

Design goals (inspired by pi):
- minimal primitives
- predictable truncation
- actionable continuation hints
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TextIO

# ---------------------------------------------------------------------------
# Limits (keep token usage low)
# ---------------------------------------------------------------------------

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024

BASH_TIMEOUT_SECONDS = 120
_BASH_MAX_IN_MEMORY_BYTES = 5_000_000


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI compatible)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Read a file. Text output is truncated to 2000 lines or 50KB. Use offset/limit to read large files."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write a file (create or overwrite).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative or absolute)."},
                    "content": {"type": "string", "description": "File content."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": (
                "Edit a file by replacing an exact oldText snippet with newText. oldText must match exactly."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the session working directory. "
                "Output is truncated; if truncated, the full output is written to a file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (optional)."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
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


def cancel_all_tools() -> None:
    """Terminate all running bash subprocesses."""

    for proc in list(_ACTIVE_PROCS):
        try:
            proc.kill()
        except Exception:
            pass
    _ACTIVE_PROCS.clear()


ToolOutputCallback = Callable[[str], None]


class ToolExecutor:
    """Execute tool calls for a single session."""

    def __init__(self, *, cwd: str, session_dir: Path):
        self.cwd = str(Path(cwd).resolve(strict=False))
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "tool-output").mkdir(parents=True, exist_ok=True)

    # ---- read -----------------------------------------------------------------

    def read(self, *, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a text file.

        offset is 1-indexed. limit is number of lines.
        """

        abs_path = resolve_path(path, cwd=self.cwd)
        p = Path(abs_path)
        if not p.exists():
            return f"error: file not found: {path}"
        if not p.is_file():
            return f"error: not a file: {path}"

        try:
            raw = p.read_bytes()
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
        abs_path = resolve_path(path, cwd=self.cwd)
        p = Path(abs_path)
        if not p.exists():
            return f"error: file not found: {path}"
        if not p.is_file():
            return f"error: not a file: {path}"

        try:
            text = p.read_text(encoding="utf-8")
        except Exception as exc:
            return f"error: failed to read file: {exc}"

        if oldText not in text:
            hint = _closest_line_hint(text, oldText)
            if hint:
                return f"error: oldText not found. closest line: {hint}"
            return "error: oldText not found"

        count = text.count(oldText)
        if count != 1:
            return f"error: oldText occurs {count} times; provide a more specific oldText"

        updated = text.replace(oldText, newText, 1)
        try:
            _atomic_write_text(p, updated)
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
        timeout = int(timeout or BASH_TIMEOUT_SECONDS)
        if timeout <= 0:
            timeout = BASH_TIMEOUT_SECONDS

        proc: subprocess.Popen[str] | None = None
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
            )
            _ACTIVE_PROCS.add(proc)

            assert proc.stdout is not None

            # Stream line-by-line
            for line in iter(proc.stdout.readline, ""):
                if line == "" and proc.poll() is not None:
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
                        full_path = self.session_dir / "tool-output" / f"bash-{tool_call_id}.log"
                        full_file = full_path.open("w", encoding="utf-8")
                        if out_lines:
                            full_file.write("\n".join(out_lines))
                            full_file.write("\n")
                            tail_lines.extend(out_lines)
                        out_lines = []
                        spilled_to_file = True

                if on_output:
                    on_output(line)

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                return f"error: timeout after {timeout}s"

            output_lines = list(tail_lines) if spilled_to_file else out_lines
            output = "\n".join(output_lines).strip() or "(empty)"
            content, trunc = truncate_text(output)

            if spilled_to_file:
                return content + _full_output_note(full_path, in_memory=True)

            if trunc.truncated:
                # Write full output to session file for later read
                full_path = self.session_dir / "tool-output" / f"bash-{tool_call_id}.log"
                try:
                    full_path.write_text(output, encoding="utf-8")
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
                _ACTIVE_PROCS.discard(proc)
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass


def parse_tool_arguments(raw: str | None) -> dict[str, Any] | str:
    """Parse tool arguments JSON.

    Returns dict on success, or an error string on failure.
    """

    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception as exc:
        return f"invalid tool arguments JSON: {exc}"
    if not isinstance(obj, dict):
        return "tool arguments must be a JSON object"
    return obj


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
