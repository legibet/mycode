"""Shared utilities used across core modules."""

from __future__ import annotations

import json
from typing import Any


def as_int(value: Any) -> int | None:
    """Return value if it is a strict int (not bool), otherwise None."""
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def as_bool(value: Any) -> bool | None:
    """Return value if it is a bool, otherwise None."""
    return value if isinstance(value, bool) else None


def omit_none(d: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of d with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def parse_tool_arguments(raw: str | None) -> dict[str, Any] | None:
    """Parse a JSON tool-arguments string.

    Returns the parsed dict, or None when the input is missing/invalid/non-object.
    Empty / None input is treated as an empty argument set.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
