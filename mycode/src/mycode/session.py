"""Session storage and timeline events (append-only JSONL).

On disk:

<data_dir>/<session_id>/
  meta.json
  messages.jsonl   # internal message/block dicts (system prompt excluded)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from mycode.messages import ConversationMessage, build_message, flatten_message_text, text_block

# ---------------------------------------------------------------------
# Session format and compacting defaults
# ---------------------------------------------------------------------

MESSAGE_FORMAT_VERSION = 6
DEFAULT_COMPACT_THRESHOLD = 0.8
DEFAULT_SESSION_TITLE = "New chat"

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
    """Session metadata persisted to meta.json.

    Excludes per-turn state like provider / model / api_base — those live on
    each ConversationMessage and would drift after ``/model`` switches.
    """

    cwd: str
    title: str
    created_at: str
    updated_at: str
    message_format_version: int


SessionMetaDict = dict[str, object]


class SessionData(TypedDict):
    session: SessionMetaDict
    messages: list[ConversationMessage]


# ---------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------


@dataclass
class SessionStore:
    """File-based session store backed by append-only JSONL files.

    ``data_dir`` is the directory under which each session lives as its own
    subdirectory named by ``session_id``. The SDK never picks a default path;
    callers (CLI/server) decide where sessions are stored.
    """

    data_dir: Path

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    # Session paths
    # ---------------------------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        return self.data_dir / session_id

    def meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def messages_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "messages.jsonl"

    def session_exists(self, session_id: str) -> bool:
        return self.meta_path(session_id).exists()

    def _read_meta(self, session_id: str) -> SessionMetaDict | None:
        path = self.meta_path(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_meta(self, session_id: str, meta: SessionMetaDict) -> None:
        self.meta_path(session_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------------
    # Session CRUD
    # ---------------------------------------------------------------------

    async def create_session(self, session_id: str, *, cwd: str) -> SessionData:
        """Create the on-disk session directory with a fresh meta.json."""

        now = _now()
        meta = asdict(
            SessionMeta(
                cwd=os.path.abspath(cwd),
                title=DEFAULT_SESSION_TITLE,
                created_at=now,
                updated_at=now,
                message_format_version=MESSAGE_FORMAT_VERSION,
            )
        )

        def write_files() -> None:
            session_dir = self.session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            self._write_meta(session_id, meta)
            self.messages_path(session_id).touch(exist_ok=True)

        await asyncio.to_thread(write_files)
        return {"session": self._summary(session_id, meta), "messages": []}

    async def list_sessions(self, *, cwd: str | None = None) -> list[SessionMetaDict]:
        """List all sessions under ``data_dir``, newest first."""

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

    def load_session_sync(self, session_id: str) -> SessionData | None:
        """Synchronous variant of :meth:`load_session`."""

        meta = self._read_meta(session_id)
        if meta is None:
            return None

        # Read the raw append-only log first. Replay happens after that.
        raw_messages: list[ConversationMessage] = []
        try:
            with self.messages_path(session_id).open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if isinstance(msg, dict):
                            raw_messages.append(cast(ConversationMessage, msg))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass

        # Replay order defines the visible conversation state.
        # 1) compact rewrites older history into one summary view
        # 2) rewind truncates that visible list by message index
        # Orphan tool_use blocks (e.g. left open by a server crash) are
        # closed by the provider adapter at replay time, not here.
        visible_messages = apply_compact(raw_messages)
        visible_messages = apply_rewind(visible_messages)

        return {"session": self._summary(session_id, meta), "messages": visible_messages}

    async def load_session(self, session_id: str) -> SessionData | None:
        return await asyncio.to_thread(self.load_session_sync, session_id)

    async def delete_session(self, session_id: str) -> None:
        def delete() -> None:
            sdir = self.session_dir(session_id)
            if sdir.exists():
                shutil.rmtree(sdir, ignore_errors=True)

        await asyncio.to_thread(delete)

    async def clear_session(self, session_id: str) -> None:
        """Drop all messages and reset derived meta to the "new chat" state."""

        def clear() -> None:
            if not self.messages_path(session_id).exists():
                return
            self.messages_path(session_id).write_text("", encoding="utf-8")
            meta = self._read_meta(session_id)
            if meta is None:
                return
            meta["title"] = DEFAULT_SESSION_TITLE
            meta["updated_at"] = _now()
            self._write_meta(session_id, meta)

        await asyncio.to_thread(clear)

    # ---------------------------------------------------------------------
    # Append-only updates
    # ---------------------------------------------------------------------

    async def append_message(self, session_id: str, message: ConversationMessage) -> None:
        """Append one message to ``messages.jsonl`` and refresh meta.

        Meta's ``updated_at`` is bumped on every call; ``title`` is promoted
        from the default placeholder on the first user message that carries
        readable text.
        """

        def append() -> None:
            with self.messages_path(session_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=False))
                handle.write("\n")

            meta = self._read_meta(session_id)
            if meta is None:
                return
            meta["updated_at"] = _now()
            if meta.get("title") == DEFAULT_SESSION_TITLE and message.get("role") == "user":
                title_text = flatten_message_text(message, include_thinking=False).replace("\n", " ").strip()
                if title_text:
                    meta["title"] = title_text[:48]
            self._write_meta(session_id, meta)

        await asyncio.to_thread(append)

    async def append_rewind(self, session_id: str, rewind_to: int) -> None:
        """Append a rewind marker to the session JSONL."""

        await self.append_message(session_id, build_rewind_event(rewind_to))

    def _summary(self, session_id: str, meta: SessionMetaDict) -> SessionMetaDict:
        """Return meta augmented with the session id for API responses."""

        return {"id": session_id, **meta}
