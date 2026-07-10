"""Shared utilities used across core modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_path(path: str, *, cwd: str) -> str:
    """Resolve ``path`` against ``cwd``; absolute and ``~`` paths pass through."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return str(p.resolve(strict=False))


def as_int(value: Any) -> int | None:
    """Return value if it is a strict int (not bool), otherwise None."""
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def as_bool(value: Any) -> bool | None:
    """Return value if it is a bool, otherwise None."""
    return value if isinstance(value, bool) else None


def omit_none(d: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of d with None values removed."""
    return {k: v for k, v in d.items() if v is not None}
