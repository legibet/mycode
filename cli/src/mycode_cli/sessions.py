"""Application-level session store: the SDK message timeline plus a catalog.

The SDK owns each session's ``messages.jsonl``; this store adds the CLI's
catalog around it — a ``meta.json`` per session carrying workspace ``cwd``,
display ``title``, and timestamps — and the listing/lifecycle operations the
TUI and web server are built on.

On disk:

<data_dir>/
  <session_id>/
    meta.json        # catalog entry: cwd, title, created_at, updated_at
    messages.jsonl   # SDK-owned message timeline
    tool-output/     # scratch area for large tool outputs

``updated_at`` tracks user-visible session changes (new user turn, rewind,
clear, manual compact), not every persisted message.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from mycode.messages import ConversationMessage
from mycode.session import SessionStore as TimelineStore

DEFAULT_SESSION_TITLE = "New chat"
_META_KEYS = ("cwd", "title", "created_at", "updated_at")

SessionMetaDict = dict[str, object]


class SessionData(TypedDict):
    session: SessionMetaDict
    messages: list[ConversationMessage]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def derive_title(text: str) -> str:
    """Session title from user input text; empty input keeps the default."""

    title = text.replace("\n", " ").strip()[:48]
    return title or DEFAULT_SESSION_TITLE


class SessionStore(TimelineStore):
    """Session catalog and timeline façade used by the TUI and web server."""

    # ------------------------------------------------------------------
    # Catalog I/O
    # ------------------------------------------------------------------

    def meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def _read_meta(self, session_id: str) -> SessionMetaDict | None:
        path = self.meta_path(session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return {key: raw[key] for key in _META_KEYS if key in raw}

    def _write_meta(self, session_id: str, meta: SessionMetaDict) -> None:
        path = self.meta_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def _summary(self, session_id: str, meta: SessionMetaDict) -> SessionMetaDict:
        """Return meta augmented with the session id for API responses."""

        return {"id": session_id, **meta}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str, *, cwd: str) -> SessionMetaDict:
        """Create the catalog entry for a fresh session and return its summary."""

        now = _now()
        meta: SessionMetaDict = {
            "cwd": os.path.abspath(cwd),
            "title": DEFAULT_SESSION_TITLE,
            "created_at": now,
            "updated_at": now,
        }
        await asyncio.to_thread(self._write_meta, session_id, meta)
        return self._summary(session_id, meta)

    async def record_user_turn(self, session_id: str, *, cwd: str, text: str) -> SessionMetaDict:
        """Register one user turn: create the catalog entry on the first turn,
        promote the title from the first turn that carries readable text, and
        bump ``updated_at``."""

        def record() -> SessionMetaDict:
            meta: SessionMetaDict | None = self._read_meta(session_id)
            if meta is None:
                now = _now()
                meta = {
                    "cwd": os.path.abspath(cwd),
                    "title": derive_title(text),
                    "created_at": now,
                    "updated_at": now,
                }
            else:
                if meta.get("title") == DEFAULT_SESSION_TITLE:
                    meta["title"] = derive_title(text)
                meta["updated_at"] = _now()
            self._write_meta(session_id, meta)
            return self._summary(session_id, meta)

        return await asyncio.to_thread(record)

    async def touch(self, session_id: str) -> None:
        """Bump ``updated_at``; a no-op for sessions without a catalog entry."""

        def bump() -> None:
            meta = self._read_meta(session_id)
            if meta is None:
                return
            meta["updated_at"] = _now()
            self._write_meta(session_id, meta)

        await asyncio.to_thread(bump)

    async def clear_session(self, session_id: str) -> None:
        """Drop all messages and reset the catalog to the "new chat" state."""

        await self.clear_messages(session_id)

        def reset() -> None:
            meta = self._read_meta(session_id)
            if meta is None:
                return
            meta["title"] = DEFAULT_SESSION_TITLE
            meta["updated_at"] = _now()
            self._write_meta(session_id, meta)

        await asyncio.to_thread(reset)

    async def delete_session(self, session_id: str) -> None:
        """Delete the whole session directory: catalog, timeline, tool output."""

        await asyncio.to_thread(shutil.rmtree, self.session_dir(session_id), True)

    # ------------------------------------------------------------------
    # Listing and loading
    # ------------------------------------------------------------------

    async def list_sessions(self, *, cwd: str | None = None) -> list[SessionMetaDict]:
        """List cataloged sessions under ``data_dir``, newest first."""

        normalized = os.path.abspath(cwd) if cwd else None

        def load_all() -> list[SessionMetaDict]:
            out: list[SessionMetaDict] = []
            for entry in self.data_dir.iterdir():
                if not entry.is_dir():
                    continue
                meta = self._read_meta(entry.name)
                if meta is None:
                    continue
                if normalized and os.path.abspath(str(meta.get("cwd") or "")) != normalized:
                    continue
                out.append(self._summary(entry.name, meta))

            out.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
            return out

        return await asyncio.to_thread(load_all)

    async def latest_session(self, *, cwd: str | None = None) -> SessionMetaDict | None:
        sessions = await self.list_sessions(cwd=cwd)
        return sessions[0] if sessions else None

    async def load_session(self, session_id: str) -> SessionData | None:
        """Load one cataloged session's meta and visible messages."""

        def load() -> SessionData | None:
            meta = self._read_meta(session_id)
            if meta is None:
                return None
            return {
                "session": self._summary(session_id, meta),
                "messages": self.load_messages_sync(session_id),
            }

        return await asyncio.to_thread(load)
