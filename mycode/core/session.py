"""Session storage (append-only JSONL).

Inspired by pi/mom design principles:
- append-only message log (JSONL)
- small metadata file per session
- no rewriting of full conversation on each turn

On disk:

mycode/data/sessions/<session_id>/
  meta.json
  messages.jsonl   # Internal message/block dicts (excluding system prompt)
  tool-output/     # large bash outputs (referenced by tool results)
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mycode.core.messages import flatten_message_text

MESSAGE_FORMAT_VERSION = 2
DEFAULT_SESSION_PROVIDER = "anthropic"


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    """File-based session store with a small in-memory cache."""

    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "data" / "sessions")

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
        now = _now()

        meta = SessionMeta(
            id=session_id,
            title=title or "New chat",
            provider=provider,
            model=model,
            cwd=cwd,
            api_base=api_base,
            message_format_version=MESSAGE_FORMAT_VERSION,
            created_at=now,
            updated_at=now,
        )

        def write_files() -> None:
            sdir = self.session_dir(session_id)
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "tool-output").mkdir(parents=True, exist_ok=True)
            self.meta_path(session_id).write_text(json.dumps(meta.__dict__, indent=2), encoding="utf-8")
            self.messages_path(session_id).touch(exist_ok=True)

        await asyncio.to_thread(write_files)

        return {"session": meta.__dict__, "messages": []}

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
            mp = self.meta_path(session_id)
            if not mp.exists():
                return None

            meta = json.loads(mp.read_text(encoding="utf-8"))

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
            mp = self.meta_path(session_id)
            if not mp.exists():
                return
            meta = json.loads(mp.read_text(encoding="utf-8"))
            meta["updated_at"] = _now()
            mp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self.messages_path(session_id).write_text("", encoding="utf-8")

        await asyncio.to_thread(clear)

    # ---------------------------------------------------------------------
    # Append-only updates
    # ---------------------------------------------------------------------

    async def append_message(self, session_id: str, message: dict) -> None:
        def append() -> None:
            sdir = self.session_dir(session_id)
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "tool-output").mkdir(parents=True, exist_ok=True)

            lp = self.messages_path(session_id)
            with lp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False))
                f.write("\n")

            # update updated_at + maybe infer title
            mp = self.meta_path(session_id)
            if not mp.exists():
                # Create minimal meta if missing
                meta = {
                    "id": session_id,
                    "title": "New chat",
                    "provider": "",
                    "model": "",
                    "cwd": "",
                    "api_base": None,
                    "message_format_version": MESSAGE_FORMAT_VERSION,
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            else:
                meta = json.loads(mp.read_text(encoding="utf-8"))

            meta["updated_at"] = _now()

            meta.setdefault("message_format_version", MESSAGE_FORMAT_VERSION)

            if meta.get("title") == "New chat" and message.get("role") == "user":
                content = flatten_message_text(message, include_thinking=False).replace("\n", " ").strip()
                if content:
                    meta["title"] = content[:48]

            mp.write_text(json.dumps(meta, indent=2), encoding="utf-8")

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
            # Keep meta in sync with the latest request config.
            # (Pi would store model changes as events; here we keep it simple.)
            def update_meta() -> None:
                mp = self.meta_path(session_id)
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    return

                changed = False
                norm_cwd = os.path.abspath(cwd)

                if meta.get("provider") != provider:
                    meta["provider"] = provider
                    changed = True
                if meta.get("model") != model:
                    meta["model"] = model
                    changed = True
                if meta.get("cwd") != norm_cwd:
                    meta["cwd"] = norm_cwd
                    changed = True
                if meta.get("api_base") != api_base:
                    meta["api_base"] = api_base
                    changed = True
                if meta.get("message_format_version") != MESSAGE_FORMAT_VERSION:
                    meta["message_format_version"] = MESSAGE_FORMAT_VERSION
                    changed = True

                if changed:
                    meta["updated_at"] = _now()
                    mp.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            await asyncio.to_thread(update_meta)
            return await self.load_session(session_id) or data

        # Create a session with a fixed ID (for compatibility with frontend default session_id).
        cwd = os.path.abspath(cwd)
        now = _now()

        def create_fixed() -> None:
            sdir = self.session_dir(session_id)
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "tool-output").mkdir(parents=True, exist_ok=True)

            meta = SessionMeta(
                id=session_id,
                title="New chat",
                provider=provider,
                model=model,
                cwd=cwd,
                api_base=api_base,
                message_format_version=MESSAGE_FORMAT_VERSION,
                created_at=now,
                updated_at=now,
            )
            self.meta_path(session_id).write_text(json.dumps(meta.__dict__, indent=2), encoding="utf-8")
            self.messages_path(session_id).touch(exist_ok=True)

        await asyncio.to_thread(create_fixed)
        return await self.load_session(session_id) or {"session": None, "messages": []}
