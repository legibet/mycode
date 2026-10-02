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
manual compact), not every persisted message.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from mycode.messages import ConversationMessage, flatten_message_text
from mycode.models import Cost, add_cost, add_usage
from mycode.session import SessionStore as TimelineStore
from mycode.session import apply_rewind

DEFAULT_SESSION_TITLE = "New chat"
_META_KEYS = ("cwd", "title", "created_at", "updated_at")

SessionMetaDict = dict[str, object]


@dataclass(frozen=True)
class SessionTotals:
    """Token usage and USD cost summed over a session's billed requests."""

    usage: dict[str, int] = field(default_factory=dict)
    cost: Cost | None = None

    def add(self, usage: dict[str, Any] | None, cost: Cost | None) -> SessionTotals:
        return SessionTotals(add_usage(self.usage, usage or {}), add_cost(self.cost, cost))

    def payload(self) -> dict[str, Any]:
        """API fields; ``None`` means unknown."""

        return {"session_usage": self.usage or None, "session_cost": self.cost}


class SessionData(TypedDict):
    session: SessionMetaDict
    messages: list[ConversationMessage]
    totals: SessionTotals


class SessionSnippet(TypedDict):
    before: str
    match: str
    after: str


class SessionSearchHit(TypedDict):
    session: SessionMetaDict
    snippet: SessionSnippet | None


def sum_session_totals(messages: Iterable[ConversationMessage]) -> SessionTotals:
    """Sum billed requests, including compact markers and rewound turns."""

    totals = SessionTotals()
    for message in messages:
        if message.get("role") in {"assistant", "compact"}:
            meta = message.get("meta") or {}
            totals = totals.add(meta.get("usage"), meta.get("cost"))
    return totals


def _now() -> str:
    return datetime.now(UTC).isoformat()


def derive_title(text: str) -> str:
    """Session title from user input text; empty input keeps the default."""

    title = text.replace("\n", " ").strip()[:48]
    return title or DEFAULT_SESSION_TITLE


@dataclass
class SessionStore(TimelineStore):
    """Session catalog and timeline façade used by the TUI and web server."""

    # Search cache derived from ``messages.jsonl``: session_id -> ((mtime_ns, size),
    # whitespace-collapsed text of each visible user/assistant message).
    _search_texts: dict[str, tuple[tuple[int, int], list[str]]] = field(default_factory=dict, init=False, repr=False)

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

    async def delete_session(self, session_id: str) -> None:
        """Delete the whole session directory: catalog, timeline, tool output."""

        self._search_texts.pop(session_id, None)
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

    async def load_metadata(self, session_id: str) -> SessionMetaDict | None:
        """Load a catalog entry without reading its message timeline."""

        def load() -> SessionMetaDict | None:
            meta = self._read_meta(session_id)
            return self._summary(session_id, meta) if meta is not None else None

        return await asyncio.to_thread(load)

    async def load_session(self, session_id: str) -> SessionData | None:
        """Load metadata, visible messages, and usage totals from one raw timeline read."""

        def load() -> SessionData | None:
            meta = self._read_meta(session_id)
            if meta is None:
                return None
            raw = self.load_raw_messages_sync(session_id)
            return {
                "session": self._summary(session_id, meta),
                "messages": apply_rewind(raw),
                "totals": sum_session_totals(raw),
            }

        return await asyncio.to_thread(load)

    def _searchable_texts(self, session_id: str) -> list[str]:
        """Visible user/assistant text per message, cached until the timeline file changes."""

        try:
            stat = self.messages_path(session_id).stat()
        except FileNotFoundError:
            return []
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._search_texts.get(session_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        # The key was taken before the read: a concurrent append leaves a stale
        # key behind, so the next search re-reads the file.
        texts = [
            " ".join(flatten_message_text(message, include_thinking=False).split())
            for message in self.load_messages_sync(session_id)
            if message.get("role") in {"user", "assistant"}
        ]
        self._search_texts[session_id] = (key, texts)
        return texts

    async def search_sessions(self, query: str, *, cwd: str | None = None, limit: int = 50) -> list[SessionSearchHit]:
        """Find sessions whose title or visible user/assistant text contains
        ``query`` (case-insensitive), newest first.

        The snippet comes from the first body match in timeline order, on
        whitespace-collapsed text; it is ``None`` when only the title matched.
        """

        needle = " ".join(query.split())
        if not needle:
            return []
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        sessions = await self.list_sessions(cwd=cwd)

        def scan() -> list[SessionSearchHit]:
            hits: list[SessionSearchHit] = []
            for session in sessions:
                snippet: SessionSnippet | None = None
                for text in self._searchable_texts(str(session["id"])):
                    if found := pattern.search(text):
                        start, end = found.span()
                        snippet = {
                            "before": text[max(0, start - 60) : start],
                            "match": found.group(),
                            "after": text[end : end + 80],
                        }
                        break
                if snippet is None and not pattern.search(str(session.get("title") or "")):
                    continue
                hits.append({"session": session, "snippet": snippet})
                if len(hits) >= limit:
                    break
            return hits

        return await asyncio.to_thread(scan)
