"""Persistent TUI preferences."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress

from mycode_cli.config import resolve_mycode_home


def load_efforts() -> dict[str, str]:
    path = resolve_mycode_home() / "tui.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}

    efforts = data.get("effort") if isinstance(data, dict) else None
    if not isinstance(efforts, dict):
        return {}
    return {key: value for key, value in efforts.items() if isinstance(key, str) and isinstance(value, str)}


def save_efforts(efforts: dict[str, str]) -> None:
    path = resolve_mycode_home() / "tui.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="tui.json.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"effort": efforts}, file, indent=2)
            file.write("\n")
        os.replace(temp_name, path)
    except Exception:
        with suppress(OSError):
            os.unlink(temp_name)
        raise
