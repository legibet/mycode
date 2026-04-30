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

1. **Task and Intent**: Describe the user's overall goal — what is being \
built, fixed, or investigated, and why.
2. **Decisions and Constraints**: List the decisions made, constraints \
discovered, and approaches chosen or rejected, with the reasoning behind \
each.
3. **User Requests**: Every distinct request or instruction the user gave, \
in chronological order. Preserve the user's original wording for ambiguous \
or nuanced requests.
4. **Files and Changes**: Enumerate every file read, modified, or created \
— paths, what changed, and any code snippets the next turn will need to \
reason about, quoted verbatim.
5. **Errors and Fixes**: List errors encountered with the original message \
verbatim, the cause if known, and the resolution — or that it remains open.
6. **Current State**: What is verified working, what is known broken, what \
is in progress.
7. **Next Step**: The next step to take, with a direct quote from the most \
recent conversation showing where the work left off.

Rules:
- Be specific: reproduce file paths, function names, error messages, and \
other identifiers verbatim — never paraphrase them.
- Do not add suggestions or opinions — only summarize what happened.
- Keep it concise but complete.\
"""

_CONTINUATION_HEADER = "This session is being continued from a previous conversation that was compacted to fit the context window. The summary below covers the earlier portion of the conversation."

_TRANSCRIPT_HINT = "For verbatim details not captured in this summary (exact code snippets, error messages, or earlier output), read the original conversation log at: {path}"

_CONTINUATION_FOOTER = 'Resume directly from where the work left off. Do not acknowledge this summary, do not recap, and do not preface with "I\'ll continue" or similar.'

_COMPACT_ACK = "Acknowledged."


# ---------------------------------------------------------------------
# Compact and rewind session events
# ---------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def should_compact(
    last_total_tokens: int | None,
    context_window: int | None,
    threshold: float,
) -> bool:
    """True when the latest call's `total_tokens` ≥ `context_window × threshold`.

    `total_tokens` already covers the next API call's prompt floor, so it is
    the right input here. The `(1 - threshold)` headroom is reserved for the
    compact LLM call itself (see docs/sessions.md).
    """

    if not last_total_tokens or not context_window or threshold <= 0:
        return False
    return last_total_tokens >= context_window * threshold


def build_compact_event(
    summary_text: str,
    *,
    provider: str,
    model: str,
    compacted_count: int,
    total_tokens: int | None = None,
) -> ConversationMessage:
    """Build the compact event stored in session JSONL."""

    meta: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "compacted_count": compacted_count,
    }
    if total_tokens is not None:
        meta["total_tokens"] = total_tokens
    return build_message("compact", [text_block(summary_text)], meta=meta)


def apply_compact(
    messages: list[ConversationMessage],
    *,
    transcript_path: str | None = None,
    continue_now: bool | None = None,
) -> list[ConversationMessage]:
    """Replace the latest compact event with a synthetic summary view.

    ``continue_now`` omits the ack and leaves a user instruction last so the
    agent loop can immediately request the next assistant response.
    """

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

    tail = messages[last_compact_index + 1 :]
    if continue_now is None:
        # During live tool-loop compaction the next persisted message is the
        # assistant continuation. Waiting compaction has no tail yet.
        continue_now = bool(tail and tail[0].get("role") == "assistant")

    parts = [_CONTINUATION_HEADER, summary_text]
    if transcript_path:
        parts.append(_TRANSCRIPT_HINT.format(path=transcript_path))
    if continue_now:
        parts.append(_CONTINUATION_FOOTER)

    result = [build_message("user", [text_block("\n\n".join(parts))], meta={"synthetic": True})]
    if not continue_now:
        result.append(build_message("assistant", [text_block(_COMPACT_ACK)], meta={"synthetic": True}))
    result.extend(tail)
    return result


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
        visible_messages = apply_compact(
            raw_messages,
            transcript_path=str(self.messages_path(session_id)),
        )
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
