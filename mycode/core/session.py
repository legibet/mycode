"""Session storage (append-only JSONL).

Inspired by pi/mom design principles:
- append-only message log (JSONL)
- small metadata file per session
- no rewriting of full conversation on each turn

On disk:

~/.mycode/sessions/<session_id>/
  meta.json
  messages.jsonl   # Internal message/block dicts (excluding system prompt)
  tool-output/     # large bash outputs (referenced by tool results)
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mycode.core.config import resolve_sessions_dir
from mycode.core.messages import flatten_message_text

MESSAGE_FORMAT_VERSION = 3
DEFAULT_SESSION_PROVIDER = "anthropic"
DEFAULT_SESSION_TITLE = "New chat"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _build_session_meta(
    session_id: str,
    *,
    title: str,
    provider: str,
    model: str,
    cwd: str,
    api_base: str | None,
) -> SessionMeta:
    now = _now()
    return SessionMeta(
        id=session_id,
        title=title,
        provider=provider,
        model=model,
        cwd=cwd,
        api_base=api_base,
        message_format_version=MESSAGE_FORMAT_VERSION,
        created_at=now,
        updated_at=now,
    )


@dataclass
class SessionMeta:
    id: str
    title: str
    provider: str
    model: str
    cwd: str
    api_base: str | None
    message_format_version: int
    created_at: str
    updated_at: str


@dataclass
class SessionStore:
    """File-based session store backed by append-only JSONL files."""

    data_dir: Path = field(default_factory=resolve_sessions_dir)

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    # Paths
    # ---------------------------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        return self.data_dir / session_id

    def meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def messages_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "messages.jsonl"

    def _ensure_session_dir(self, session_id: str) -> None:
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "tool-output").mkdir(parents=True, exist_ok=True)

    def _read_meta(self, session_id: str) -> dict | None:
        path = self.meta_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_meta(self, session_id: str, meta: dict) -> None:
        self.meta_path(session_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------------
    # CRUD
    # ---------------------------------------------------------------------

    async def create_session(
        self,
        title: str | None,
        *,
        provider: str = DEFAULT_SESSION_PROVIDER,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> dict:
        session_id = uuid4().hex
        cwd = os.path.abspath(cwd)
        meta = asdict(
            _build_session_meta(
                session_id,
                title=title or DEFAULT_SESSION_TITLE,
                provider=provider,
                model=model,
                cwd=cwd,
                api_base=api_base,
            )
        )

        def write_files() -> None:
            self._ensure_session_dir(session_id)
            self._write_meta(session_id, meta)
            self.messages_path(session_id).touch(exist_ok=True)

        await asyncio.to_thread(write_files)

        return {"session": meta, "messages": []}

    async def list_sessions(self, *, cwd: str | None = None) -> list[dict]:
        normalized = os.path.abspath(cwd) if cwd else None

        def load_all() -> list[dict]:
            out: list[dict] = []
            for entry in self.data_dir.iterdir():
                if not entry.is_dir():
                    continue
                mp = entry / "meta.json"
                if not mp.exists():
                    continue
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                    if normalized and os.path.abspath(meta.get("cwd") or "") != normalized:
                        continue
                    out.append(meta)
                except Exception:
                    continue

            out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
            return out

        return await asyncio.to_thread(load_all)

    async def latest_session(self, *, cwd: str | None = None) -> dict | None:
        sessions = await self.list_sessions(cwd=cwd)
        return sessions[0] if sessions else None

    async def load_session(self, session_id: str) -> dict | None:
        def load() -> dict | None:
            meta = self._read_meta(session_id)
            if meta is None:
                return None

            msgs: list[dict] = []
            lp = self.messages_path(session_id)
            if lp.exists():
                for line in lp.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if isinstance(msg, dict):
                            msgs.append(msg)
                    except Exception:
                        continue

            return {"session": meta, "messages": msgs}

        return await asyncio.to_thread(load)

    async def delete_session(self, session_id: str) -> None:
        def delete() -> None:
            sdir = self.session_dir(session_id)
            if not sdir.exists():
                return
            # small recursive delete (no shutil.rmtree to keep deps minimal)
            for p in sorted(sdir.rglob("*"), reverse=True):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        p.rmdir()
                except Exception:
                    pass
            try:
                sdir.rmdir()
            except Exception:
                pass

        await asyncio.to_thread(delete)

    async def clear_session(self, session_id: str) -> None:
        def clear() -> None:
            meta = self._read_meta(session_id)
            if meta is None:
                return
            meta["updated_at"] = _now()
            self._write_meta(session_id, meta)
            self.messages_path(session_id).write_text("", encoding="utf-8")

        await asyncio.to_thread(clear)

    # ---------------------------------------------------------------------
    # Append-only updates
    # ---------------------------------------------------------------------

    async def append_message(self, session_id: str, message: dict) -> None:
        def append() -> None:
            self._ensure_session_dir(session_id)

            lp = self.messages_path(session_id)
            with lp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False))
                f.write("\n")

            # update updated_at + maybe infer title
            meta = self._read_meta(session_id)
            if meta is None:
                meta = asdict(
                    _build_session_meta(
                        session_id,
                        title=DEFAULT_SESSION_TITLE,
                        provider="",
                        model="",
                        cwd="",
                        api_base=None,
                    )
                )

            meta["updated_at"] = _now()

            meta.setdefault("message_format_version", MESSAGE_FORMAT_VERSION)

            if meta.get("title") == DEFAULT_SESSION_TITLE and message.get("role") == "user":
                content = flatten_message_text(message, include_thinking=False).replace("\n", " ").strip()
                if content:
                    meta["title"] = content[:48]

            self._write_meta(session_id, meta)

        await asyncio.to_thread(append)

    async def get_or_create(
        self,
        session_id: str,
        *,
        provider: str = DEFAULT_SESSION_PROVIDER,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> dict:
        """Get an existing session, or create it if missing."""

        data = await self.load_session(session_id)
        if data:
            return data

        # Create a session with a fixed ID (for compatibility with frontend default session_id).
        cwd = os.path.abspath(cwd)
        meta = asdict(
            _build_session_meta(
                session_id,
                title=DEFAULT_SESSION_TITLE,
                provider=provider,
                model=model,
                cwd=cwd,
                api_base=api_base,
            )
        )

        def create_fixed() -> None:
            self._ensure_session_dir(session_id)
            self._write_meta(session_id, meta)
            self.messages_path(session_id).touch(exist_ok=True)

        await asyncio.to_thread(create_fixed)
        return await self.load_session(session_id) or {"session": None, "messages": []}
