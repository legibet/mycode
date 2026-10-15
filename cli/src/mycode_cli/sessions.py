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
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from mycode.messages import ConversationMessage, RewindMarker
from mycode.session import SessionStore as TimelineStore
from mycode.session import apply_rewind
from mycode.usage import Cost, Usage, add_cost, add_usage
from mycode_cli.config import resolve_cwd

DEFAULT_SESSION_TITLE = "New chat"
_META_FIELDS = ("cwd", "title", "created_at", "updated_at")


class SessionInfo(TypedDict):
    """A session's catalog entry; ``meta.json`` stores every field but ``id``."""

    id: str
    cwd: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SessionTotals:
    """Token usage and USD cost summed over a session's billed requests."""

    usage: Usage = field(default_factory=Usage)
    cost: Cost | None = None

    def add(self, usage: Usage | None, cost: Cost | None) -> SessionTotals:
        return SessionTotals(add_usage(self.usage, usage or {}), add_cost(self.cost, cost))

    def payload(self) -> dict[str, Any]:
        """API fields; ``None`` means unknown."""

        return {"session_usage": self.usage or None, "session_cost": self.cost}


class SessionData(TypedDict):
    session: SessionInfo
    messages: list[ConversationMessage]
    totals: SessionTotals


class SessionSnippet(TypedDict):
    before: str
    match: str
    after: str


class SessionSearchHit(TypedDict):
    session: SessionInfo
    snippet: SessionSnippet | None


def sum_session_totals(records: Iterable[ConversationMessage | RewindMarker]) -> SessionTotals:
    """Sum billed requests, including compact markers and rewound turns."""

    totals = SessionTotals()
    for record in records:
        match record:
            case {"role": "assistant" | "compact"}:
                meta = record.get("meta", {})
                totals = totals.add(meta.get("usage"), meta.get("cost"))
    return totals


def _now() -> str:
    return datetime.now(UTC).isoformat()


def display_text(message: ConversationMessage) -> str:
    """Join the text a person wrote or reads in ``message`` as paragraphs.

    Attachment payloads, skill snapshots (``meta.skill_snapshot``), and
    background job notifications (``meta.job``) are replayed to the provider
    but are not message text, so they are skipped.
    """

    parts: list[str] = []
    for block in message["content"]:
        if block["type"] != "text":
            continue
        meta = block.get("meta", {})
        if meta.get("attachment") or meta.get("skill_snapshot") or meta.get("job"):
            continue
        if text := block["text"].strip():
            parts.append(text)
    return "\n\n".join(parts)


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

    def tool_output_dir(self, session_id: str) -> Path:
        """The session's scratch directory for large tool outputs and uploads."""

        return self.session_dir(session_id) / "tool-output"

    def _read_meta(self, session_id: str) -> SessionInfo | None:
        """Read a catalog entry; a missing or malformed ``meta.json`` means no entry."""

        try:
            raw = json.loads(self.meta_path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) for key in _META_FIELDS):
            return None
        return {
            "id": session_id,
            "cwd": raw["cwd"],
            "title": raw["title"],
            "created_at": raw["created_at"],
            "updated_at": raw["updated_at"],
        }

    def _write_meta(self, session: SessionInfo) -> None:
        path = self.meta_path(session["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {key: value for key, value in session.items() if key != "id"}
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def _new_session(self, session_id: str, *, cwd: str, title: str) -> SessionInfo:
        now = _now()
        return {"id": session_id, "cwd": resolve_cwd(cwd), "title": title, "created_at": now, "updated_at": now}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str, *, cwd: str) -> SessionInfo:
        """Create the catalog entry for a fresh session."""

        session = self._new_session(session_id, cwd=cwd, title=DEFAULT_SESSION_TITLE)
        await asyncio.to_thread(self._write_meta, session)
        return session

    async def record_user_turn(self, session_id: str, *, cwd: str, text: str) -> SessionInfo:
        """Register one user turn: create the catalog entry on the first turn,
        promote the title from the first turn that carries readable text, and
        bump ``updated_at``."""

        def record() -> SessionInfo:
            session = self._read_meta(session_id)
            if session is None:
                session = self._new_session(session_id, cwd=cwd, title=derive_title(text))
            else:
                if session["title"] == DEFAULT_SESSION_TITLE:
                    session["title"] = derive_title(text)
                session["updated_at"] = _now()
            self._write_meta(session)
            return session

        return await asyncio.to_thread(record)

    async def touch(self, session_id: str) -> None:
        """Bump ``updated_at``; a no-op for sessions without a catalog entry."""

        def bump() -> None:
            session = self._read_meta(session_id)
            if session is None:
                return
            session["updated_at"] = _now()
            self._write_meta(session)

        await asyncio.to_thread(bump)

    async def delete_session(self, session_id: str) -> None:
        """Delete the whole session directory: catalog, timeline, tool output."""

        self._search_texts.pop(session_id, None)
        await asyncio.to_thread(shutil.rmtree, self.session_dir(session_id), True)

    # ------------------------------------------------------------------
    # Listing and loading
    # ------------------------------------------------------------------

    async def list_sessions(self, *, cwd: str | None = None) -> list[SessionInfo]:
        """List cataloged sessions under ``data_dir``, newest first."""

        normalized = resolve_cwd(cwd) if cwd else None

        def load_all() -> list[SessionInfo]:
            sessions = [
                session
                for entry in self.data_dir.iterdir()
                if entry.is_dir() and (session := self._read_meta(entry.name)) is not None
            ]
            if normalized:
                # Entries written before cwd was symlink-resolved still match.
                sessions = [session for session in sessions if resolve_cwd(session["cwd"]) == normalized]
            return sorted(sessions, key=lambda session: session["updated_at"], reverse=True)

        return await asyncio.to_thread(load_all)

    async def latest_session(self, *, cwd: str | None = None) -> SessionInfo | None:
        sessions = await self.list_sessions(cwd=cwd)
        return sessions[0] if sessions else None

    async def load_metadata(self, session_id: str) -> SessionInfo | None:
        """Load a catalog entry without reading its message timeline."""

        return await asyncio.to_thread(self._read_meta, session_id)

    async def load_totals(self, session_id: str) -> SessionTotals:
        """Sum the session's usage and USD cost over its raw timeline.

        Counts tool loops, compact summaries, and turns discarded by rewind.
        """

        return sum_session_totals(await self.load_raw_messages(session_id))

    async def load_session(self, session_id: str) -> SessionData | None:
        """Load metadata, visible messages, and usage totals from one raw timeline read."""

        def load() -> SessionData | None:
            session = self._read_meta(session_id)
            if session is None:
                return None
            raw = self.load_raw_messages_sync(session_id)
            return {"session": session, "messages": apply_rewind(raw), "totals": sum_session_totals(raw)}

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
            " ".join(display_text(message).split())
            for message in self.load_messages_sync(session_id)
            if message["role"] in {"user", "assistant"}
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
                for text in self._searchable_texts(session["id"]):
                    if found := pattern.search(text):
                        start, end = found.span()
                        snippet = {
                            "before": text[max(0, start - 60) : start],
                            "match": found.group(),
                            "after": text[end : end + 80],
                        }
                        break
                if snippet is None and not pattern.search(session["title"]):
                    continue
                hits.append({"session": session, "snippet": snippet})
                if len(hits) >= limit:
                    break
            return hits

        return await asyncio.to_thread(scan)
