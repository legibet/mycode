"""Conversation rewind support.

A rewind event is an append-only JSONL marker that tells the loader to
truncate the message list back to a prior point.  Old messages are preserved
in the file for forensic inspection but are excluded from the loaded
conversation.

The loader first applies compact so rewind indices match the visible
conversation, then applies rewind inline to truncate that visible list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mycode.core.messages import ConversationMessage


def build_rewind_event(rewind_to: int) -> ConversationMessage:
    """Build a rewind marker to append to session JSONL."""
    return {
        "role": "rewind",
        "meta": {
            "rewind_to": rewind_to,
            "created_at": datetime.now(UTC).isoformat(),
        },
    }


def apply_rewind(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Process rewind events inline — truncate when encountered.

    Scans the raw JSONL message list sequentially.  When a ``role: "rewind"``
    entry is found, the accumulated messages are truncated to
    ``meta.rewind_to`` and loading continues with subsequent lines.
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "rewind":
            rewind_to = (msg.get("meta") or {}).get("rewind_to", 0)
            result = result[:rewind_to]
        else:
            result.append(msg)
    return result
