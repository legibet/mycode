"""Session storage and timeline events (append-only JSONL).

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
from typing import Any
from uuid import uuid4

from mycode.core.config import resolve_sessions_dir
from mycode.core.messages import ConversationMessage, build_message, flatten_message_text, text_block, tool_result_block

# ---------------------------------------------------------------------
# Session format and compacting defaults
# ---------------------------------------------------------------------

MESSAGE_FORMAT_VERSION = 5
DEFAULT_SESSION_PROVIDER = "anthropic"
DEFAULT_SESSION_TITLE = "New chat"
DEFAULT_COMPACT_THRESHOLD = 0.8

COMPACT_SUMMARY_PROMPT = """\
Summarize this conversation to create a continuation document. \
This summary will replace the full conversation history, so it must \
capture everything needed to continue the work seamlessly.

Include:

1. **User Requests**: Every distinct request or instruction the user gave, \
in chronological order. Preserve the user's original wording for ambiguous \
or nuanced requests.
2. **Completed Work**: What was accomplished — files created, modified, or \
deleted; bugs fixed; features added. Include file paths and function names.
3. **Current State**: The exact state of the work right now — what is working, \
what is broken, what is partially done.
4. **Key Decisions**: Important decisions made, constraints discovered, \
approaches chosen or rejected, and why.
5. **Next Steps**: What remains to be done, any work that was in progress \
when this summary was generated.

Rules:
- Be specific: include file paths, function names, error messages, and \
concrete details.
- Do not add suggestions or opinions — only summarize what happened.
- Keep it concise but complete.\
"""

_COMPACT_ACK = "Understood. I have the context from the conversation summary and will continue the work."


# ---------------------------------------------------------------------
# Compact and rewind session events
# ---------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def should_compact(
    last_usage: dict[str, Any] | None,
    context_window: int | None,
    threshold: float,
) -> bool:
    """Return True when the last response input tokens exceed the threshold."""

    if not last_usage or not context_window or threshold <= 0:
        return False

    # Providers report prompt/input usage under slightly different field names.
    input_tokens = int(
        last_usage.get("input_tokens") or last_usage.get("prompt_tokens") or last_usage.get("prompt_token_count") or 0
    )
    return input_tokens >= context_window * threshold


def build_compact_event(
    summary_text: str,
    *,
    provider: str,
    model: str,
    compacted_count: int,
    usage: dict[str, Any] | None = None,
) -> ConversationMessage:
    """Build the compact event stored in session JSONL."""

    meta: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "compacted_count": compacted_count,
    }
    if usage is not None:
        meta["usage"] = usage
    return build_message("compact", [text_block(summary_text)], meta=meta)


def apply_compact(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Replace the latest compact event with a summary + synthetic ack."""

    # Only the newest compact event matters. Older history before it is no
    # longer visible once the summary replaces that earlier conversation.
    last_compact_index: int | None = None
    for index, message in enumerate(messages):
        if message.get("role") == "compact":
            last_compact_index = index

    if last_compact_index is None:
        return messages

    summary_text = ""
    for block in messages[last_compact_index].get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            summary_text = str(block.get("text") or "")
            break

    return [
        build_message(
            "user",
            [text_block(f"[Conversation Summary]\n\n{summary_text}")],
            meta={"synthetic": True},
        ),
        build_message("assistant", [text_block(_COMPACT_ACK)], meta={"synthetic": True}),
        *messages[last_compact_index + 1 :],
    ]


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
# Session metadata
# ---------------------------------------------------------------------


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


# ---------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------


@dataclass
class SessionStore:
    """File-based session store backed by append-only JSONL files."""

    data_dir: Path = field(default_factory=resolve_sessions_dir)

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    # Session paths and small JSON helpers
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
    # Session CRUD and loading
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
        now = _now()
        meta = asdict(
            SessionMeta(
                id=session_id,
                title=title or DEFAULT_SESSION_TITLE,
                provider=provider,
                model=model,
                cwd=os.path.abspath(cwd),
                api_base=api_base,
                message_format_version=MESSAGE_FORMAT_VERSION,
                created_at=now,
                updated_at=now,
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

            # Read the raw append-only log first. Replay happens after that.
            raw_messages: list[dict] = []
            messages_path = self.messages_path(session_id)
            try:
                with messages_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            if isinstance(msg, dict):
                                raw_messages.append(msg)
                        except Exception:
                            continue
            except FileNotFoundError:
                pass

            # Replay order defines the visible conversation state.
            # 1) compact rewrites older history into one summary view
            # 2) rewind truncates that visible list by message index
            # 3) interrupted tool repair patches the final visible state
            visible_messages = apply_compact(raw_messages)
            visible_messages = apply_rewind(visible_messages)
            self._repair_interrupted_tool_loop(session_id, meta, visible_messages)

            return {"session": meta, "messages": visible_messages}

        return await asyncio.to_thread(load)

    def _repair_interrupted_tool_loop(self, session_id: str, meta: dict, messages: list[dict]) -> None:
        """Append a synthetic tool result when the latest tool loop was interrupted.

        The runtime persists sessions as append-only JSONL. If a previous run was
        interrupted after an assistant emitted `tool_use` blocks but before a
        matching `tool_result` user message was written, repair the session by
        appending one synthetic error result message.
        """

        pending_tool_use_ids: list[str] = []
        pending_tool_call_index: int | None = None

        # Find the latest assistant message that started a tool loop.
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

            pending_tool_use_ids = tool_use_ids
            pending_tool_call_index = index
            break

        if pending_tool_call_index is None:
            return

        # Then collect tool results that were actually recorded after it.
        completed_tool_use_ids: set[str] = set()
        for message in messages[pending_tool_call_index + 1 :]:
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
                    completed_tool_use_ids.add(tool_use_id)

        missing_tool_use_ids = [
            tool_use_id for tool_use_id in pending_tool_use_ids if tool_use_id not in completed_tool_use_ids
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
            messages_path = self.messages_path(session_id)
            with messages_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False))
                handle.write("\n")

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
                now = _now()
                meta = asdict(
                    SessionMeta(
                        id=session_id,
                        title=DEFAULT_SESSION_TITLE,
                        provider=provider,
                        model=model,
                        cwd=os.path.abspath(cwd),
                        api_base=api_base,
                        message_format_version=MESSAGE_FORMAT_VERSION,
                        created_at=now,
                        updated_at=now,
                    )
                )

            messages_path = self.messages_path(session_id)
            with messages_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=False))
                handle.write("\n")

            meta["updated_at"] = _now()
            meta.setdefault("message_format_version", MESSAGE_FORMAT_VERSION)

            if meta.get("title") == DEFAULT_SESSION_TITLE and message.get("role") == "user":
                # Keep the default title until we see the first real user text,
                # then promote a short preview into the session title.
                title_text = flatten_message_text(message, include_thinking=False).replace("\n", " ").strip()
                if title_text:
                    meta["title"] = title_text[:48]

            self._write_meta(session_id, meta)

        await asyncio.to_thread(append)
