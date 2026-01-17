import glob as globlib
import os
import re
import subprocess
from collections.abc import Callable

# Track active subprocesses for cancellation
_active_processes: set[subprocess.Popen] = set()


def cancel_all_tools() -> None:
    """Terminate all running tool processes."""
    for proc in list(_active_processes):
        try:
            proc.kill()
        except Exception:
            pass
    _active_processes.clear()


def read(path: str, offset: int | None = None, limit: int | None = None) -> str:
    """Read file with line numbers."""
    try:
        if not os.path.isfile(path):
            return f"error: '{path}' is not a file"
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        start = offset or 0
        count = limit or len(lines)
        selected = lines[start : start + count]
        return "".join(f"{start + idx + 1:4}| {line}" for idx, line in enumerate(selected))
    except Exception as exc:
        return f"error: {exc}"


def write(path: str, content: str) -> str:
    """Write content to file."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


def edit(path: str, old: str, new: str, replace_all: bool | None = None) -> str:
    """Replace old with new in file."""
    try:
        if not os.path.isfile(path):
            return f"error: '{path}' is not a file"
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if old not in text:
            return "error: old_string not found"
        count = text.count(old)
        if not replace_all and count > 1:
            return f"error: old_string appears {count} times, use replace_all=true"
        replacement = text.replace(old, new, -1 if replace_all else 1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(replacement)
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


def glob(pat: str, path: str | None = None) -> str:
    """Find files by pattern."""
    try:
        base = path or "."
        files = globlib.glob(f"{base}/{pat}".replace("//", "/"), recursive=True)
        files = sorted(
            files,
            key=lambda name: os.path.getmtime(name) if os.path.isfile(name) else 0,
            reverse=True,
        )
        return "\n".join(files) or "none"
    except Exception as exc:
        return f"error: {exc}"


def grep(pat: str, path: str | None = None) -> str:
    """Search files for regex pattern."""
    try:
        pattern = re.compile(pat)
    except re.error as exc:
        return f"error: invalid regex: {exc}"
    hits: list[str] = []
    for fp in globlib.glob(f"{path or '.'}/**", recursive=True):
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    if pattern.search(line):
                        hits.append(f"{fp}:{line_no}:{line.rstrip()}")
                        if len(hits) >= 50:
                            return "\n".join(hits)
        except Exception:
            continue
    return "\n".join(hits) or "none"


def bash(cmd: str, on_output: Callable[[str], None] | None = None) -> str:
    """Run shell command with optional streaming callback."""
    lines: list[str] = []
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _active_processes.add(proc)
        if not proc.stdout:
            return "error: failed to open subprocess stdout"
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                lines.append(line)
                if on_output:
                    on_output(line.rstrip())
        proc.wait()
        return "".join(lines).strip() or "(empty)"
    except Exception as exc:
        return f"error: {exc}"
    finally:
        if proc:
            _active_processes.discard(proc)


TOOLS = [read, write, edit, glob, grep, bash]
TOOL_MAP = {fn.__name__: fn for fn in TOOLS}
