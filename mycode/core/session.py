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

from mycode.core.compact import apply_compact
from mycode.core.config import resolve_sessions_dir
from mycode.core.messages import build_message, flatten_message_text, tool_result_block
from mycode.core.rewind import apply_rewind, build_rewind_event

MESSAGE_FORMAT_VERSION = 5
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

    def draft_session(
        self,
        title: str | None,
        *,
        provider: str = DEFAULT_SESSION_PROVIDER,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> dict:
        session_id = uuid4().hex
        meta = asdict(
            _build_session_meta(
                session_id,
                title=title or DEFAULT_SESSION_TITLE,
                provider=provider,
                model=model,
                cwd=os.path.abspath(cwd),
                api_base=api_base,
            )
        )
        return {"session": meta, "messages": []}

    async def create_session(
        self,
        title: str | None,
        *,
        session_id: str | None = None,
        provider: str = DEFAULT_SESSION_PROVIDER,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> dict:
        data = self.draft_session(
            title,
            provider=provider,
            model=model,
            cwd=cwd,
            api_base=api_base,
        )
        session = data["session"]
        if session_id:
            session["id"] = session_id
        session_id = str(session["id"])

        def write_files() -> None:
            self._ensure_session_dir(session_id)
            self._write_meta(session_id, session)
            self.messages_path(session_id).touch(exist_ok=True)

        await asyncio.to_thread(write_files)
        return data

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
            try:
                with lp.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            if isinstance(msg, dict):
                                msgs.append(msg)
                        except Exception:
                            continue
            except FileNotFoundError:
                pass

            # Order matters: compact first (so rewind indices match the
            # post-compact list clients actually see), then rewind, then
            # repair any interrupted tool loops in the final logical set.
            msgs = apply_compact(msgs)
            msgs = apply_rewind(msgs)
            self._repair_interrupted_tool_loop(session_id, meta, msgs)

            return {"session": meta, "messages": msgs}

        return await asyncio.to_thread(load)

    def _repair_interrupted_tool_loop(self, session_id: str, meta: dict, messages: list[dict]) -> None:
        """Append a synthetic tool result when the latest tool loop was interrupted.

        The runtime persists sessions as append-only JSONL. If a previous run was
        interrupted after an assistant emitted `tool_use` blocks but before a
        matching `tool_result` user message was written, repair the session by
        appending one synthetic error result message.
        """

        last_tool_use_ids: list[str] = []
        last_assistant_index: int | None = None

        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant":
                continue

            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue

            tool_use_ids = [
                str(block.get("id") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
            ]
            if not tool_use_ids:
                continue

            last_tool_use_ids = tool_use_ids
            last_assistant_index = index
            break

        if last_assistant_index is None:
            return

        seen_tool_result_ids: set[str] = set()
        for message in messages[last_assistant_index + 1 :]:
            if message.get("role") != "user":
                continue

            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue

            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id") or "")
                if tool_use_id:
                    seen_tool_result_ids.add(tool_use_id)

        missing_tool_use_ids = [
            tool_use_id for tool_use_id in last_tool_use_ids if tool_use_id not in seen_tool_result_ids
        ]
        if not missing_tool_use_ids:
            return

        repair_message = build_message(
            "user",
            [
                tool_result_block(
                    tool_use_id=tool_use_id,
                    model_text="error: tool call was interrupted (no result recorded)",
                    display_text="Tool call was interrupted before it returned a result",
                    is_error=True,
                )
                for tool_use_id in missing_tool_use_ids
            ],
        )

        with self.messages_path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(repair_message, ensure_ascii=False))
            handle.write("\n")

        meta["updated_at"] = _now()
        self._write_meta(session_id, meta)
        messages.append(repair_message)

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

    async def append_rewind(self, session_id: str, rewind_to: int) -> None:
        """Append a rewind marker to the session JSONL."""

        def append() -> None:
            meta = self._read_meta(session_id)
            if meta is None:
                return
            event = build_rewind_event(rewind_to)
            lp = self.messages_path(session_id)
            with lp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False))
                f.write("\n")
            meta["updated_at"] = _now()
            self._write_meta(session_id, meta)

        await asyncio.to_thread(append)

    async def append_message(
        self,
        session_id: str,
        message: dict,
        *,
        provider: str,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> None:
        def append() -> None:
            meta = self._read_meta(session_id)
            if meta is None:
                # Create the on-disk session only when the first message is persisted.
                self._ensure_session_dir(session_id)
                meta = asdict(
                    _build_session_meta(
                        session_id,
                        title=DEFAULT_SESSION_TITLE,
                        provider=provider,
                        model=model,
                        cwd=os.path.abspath(cwd),
                        api_base=api_base,
                    )
                )

            lp = self.messages_path(session_id)
            with lp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False))
                f.write("\n")

            meta["updated_at"] = _now()

            meta.setdefault("message_format_version", MESSAGE_FORMAT_VERSION)

            if meta.get("title") == DEFAULT_SESSION_TITLE and message.get("role") == "user":
                content = flatten_message_text(message, include_thinking=False).replace("\n", " ").strip()
                if content:
                    meta["title"] = content[:48]

            self._write_meta(session_id, meta)

        await asyncio.to_thread(append)
