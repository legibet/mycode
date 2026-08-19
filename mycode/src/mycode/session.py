"""Per-session message timelines (append-only JSONL).

On disk:

<data_dir>/
  <session_id>/
    messages.jsonl   # internal message/block dicts (system prompt excluded)

The SDK owns only the message timeline. Applications are free to keep their
own files (metadata, tool output, …) next to it in the session directory.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from mycode.messages import ConversationMessage


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------
# Rewind markers
# ---------------------------------------------------------------------


def build_rewind_event(rewind_to: int) -> ConversationMessage:
    """Build a rewind marker to append to session JSONL."""

    return {
        "role": "rewind",
        "meta": {
            "rewind_to": rewind_to,
            "created_at": _now(),
        },
    }


def apply_rewind(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Apply rewind markers inline while replaying the raw message log."""

    result: list[ConversationMessage] = []
    for message in messages:
        if message.get("role") == "rewind":
            # Rewind indices refer to the visible message list at that moment,
            # so replay truncates the accumulated result in place.
            rewind_to = (message.get("meta") or {}).get("rewind_to", 0)
            result = result[:rewind_to]
        else:
            result.append(message)
    return result


# ---------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------


@dataclass
class SessionStore:
    """File-based store of per-session message timelines.

    ``data_dir`` is the directory under which each session lives as its own
    subdirectory named by ``session_id``. The SDK never picks a default path;
    callers (CLI/server) decide where sessions are stored.
    """

    data_dir: Path

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self.data_dir / session_id

    def messages_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "messages.jsonl"

    def session_exists(self, session_id: str) -> bool:
        return self.messages_path(session_id).exists()

    def load_raw_messages_sync(self, session_id: str) -> list[ConversationMessage]:
        """Read the raw JSONL timeline — rewound tails included; [] when absent.

        This exposes the append-only record as-is; callers needing the visible
        history should use :meth:`load_messages` instead.
        """

        raw_messages: list[ConversationMessage] = []
        try:
            with self.messages_path(session_id).open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        raw_messages.append(cast(ConversationMessage, msg))
        except FileNotFoundError:
            pass
        return raw_messages

    async def load_raw_messages(self, session_id: str) -> list[ConversationMessage]:
        return await asyncio.to_thread(self.load_raw_messages_sync, session_id)

    def load_messages_sync(self, session_id: str) -> list[ConversationMessage]:
        """Return the visible history: raw JSONL minus rewound tails.

        `compact` markers stay inline; the agent substitutes them when calling
        the provider. Orphan tool_use blocks (e.g. left open by a server
        crash) are closed by the provider adapter at replay time, not here.
        """

        return apply_rewind(self.load_raw_messages_sync(session_id))

    async def load_messages(self, session_id: str) -> list[ConversationMessage]:
        return await asyncio.to_thread(self.load_messages_sync, session_id)

    async def append_message(self, session_id: str, message: ConversationMessage) -> None:
        """Append one message, creating the session directory on first write."""

        def append() -> None:
            path = self.messages_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")

        await asyncio.to_thread(append)

    async def append_rewind(self, session_id: str, rewind_to: int) -> None:
        """Append a rewind marker to the session JSONL."""

        await self.append_message(session_id, build_rewind_event(rewind_to))

    async def clear_messages(self, session_id: str) -> None:
        """Drop the session's timeline; a no-op when nothing is on disk."""

        def clear() -> None:
            path = self.messages_path(session_id)
            if path.exists():
                path.write_text("", encoding="utf-8")

        await asyncio.to_thread(clear)
