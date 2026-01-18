from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agent.core import Agent


@dataclass
class SessionRecord:
    id: str
    title: str
    model: str
    cwd: str
    api_base: str | None
    messages_json: str
    created_at: str
    updated_at: str


@dataclass
class SessionStore:
    sessions: dict[str, Agent] = field(default_factory=dict)
    db_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "data" / "sessions.db")

    def __post_init__(self) -> None:
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    model TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    api_base TEXT,
                    messages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _utc_now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _load_record(self, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, title, model, cwd, api_base, messages_json, created_at, updated_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return SessionRecord(**dict(row))

    def _list_records(self) -> list[SessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, model, cwd, api_base, messages_json, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [SessionRecord(**dict(row)) for row in rows]

    def _insert_record(self, record: SessionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, title, model, cwd, api_base, messages_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.title,
                    record.model,
                    record.cwd,
                    record.api_base,
                    record.messages_json,
                    record.created_at,
                    record.updated_at,
                ),
            )
            conn.commit()

    def _update_record(self, record: SessionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, model = ?, cwd = ?, api_base = ?, messages_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    record.title,
                    record.model,
                    record.cwd,
                    record.api_base,
                    record.messages_json,
                    record.updated_at,
                    record.id,
                ),
            )
            conn.commit()

    def _delete_record(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()

    def _decode_messages(self, messages_json: str, fallback: list[dict]) -> list[dict]:
        try:
            return json.loads(messages_json)
        except json.JSONDecodeError:
            return fallback

    def _encode_messages(self, messages: list[dict]) -> str:
        return json.dumps(messages)

    def _infer_title(self, messages: list[dict]) -> str:
        for message in messages:
            if message.get("role") == "user":
                content = (message.get("content") or "").strip().replace("\n", " ")
                if content:
                    return content[:48]
        return "New chat"

    def _parse_tool_args(self, raw_args: str) -> dict:
        if not raw_args:
            return {}
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            return {}

    def _build_ui_messages(self, messages: list[dict]) -> list[dict]:
        ui_messages: list[dict] = []
        current_assistant: dict | None = None
        tool_index: dict[str, int] = {}

        def ensure_assistant() -> dict:
            nonlocal current_assistant, tool_index
            if current_assistant is None:
                current_assistant = {"role": "assistant", "parts": []}
                ui_messages.append(current_assistant)
                tool_index = {}
            return current_assistant

        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                ui_messages.append(
                    {
                        "role": "user",
                        "parts": [{"type": "text", "content": message.get("content", "")}],
                    }
                )
                current_assistant = None
                tool_index = {}
                continue
            if role == "assistant":
                assistant = ensure_assistant()
                content = message.get("content")
                if content:
                    assistant["parts"].append({"type": "text", "content": content})
                for tool_call in message.get("tool_calls", []) or []:
                    tool_id = tool_call.get("id")
                    tool_fn = tool_call.get("function", {})
                    part = {
                        "type": "tool",
                        "id": tool_id,
                        "name": tool_fn.get("name"),
                        "args": self._parse_tool_args(tool_fn.get("arguments", "")),
                        "result": "",
                        "pending": False,
                    }
                    assistant["parts"].append(part)
                    if tool_id:
                        tool_index[tool_id] = len(assistant["parts"]) - 1
                continue
            if role == "tool":
                assistant = ensure_assistant()
                tool_call_id = message.get("tool_call_id")
                content = message.get("content", "")
                if tool_call_id and tool_call_id in tool_index:
                    part_index = tool_index[tool_call_id]
                    assistant["parts"][part_index]["result"] = content
                else:
                    assistant["parts"].append(
                        {
                            "type": "tool",
                            "id": tool_call_id,
                            "name": "tool",
                            "args": {},
                            "result": content,
                            "pending": False,
                        }
                    )
        return ui_messages

    async def get_or_create(
        self,
        session_id: str,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> Agent:
        normalized_cwd = os.path.abspath(cwd)
        agent = self.sessions.get(session_id)
        if agent and (agent.model != model or agent.cwd != normalized_cwd or agent.api_base != api_base):
            agent = Agent(model=model, cwd=normalized_cwd, api_base=api_base)
            self.sessions[session_id] = agent
        if agent:
            return agent
        record = await asyncio.to_thread(self._load_record, session_id)
        if record:
            agent = Agent(model=model, cwd=normalized_cwd, api_base=api_base)
            agent.messages = self._decode_messages(record.messages_json, agent.messages)
            self.sessions[session_id] = agent
            return agent
        agent = Agent(model=model, cwd=normalized_cwd, api_base=api_base)
        now = self._utc_now()
        record = SessionRecord(
            id=session_id,
            title="New chat",
            model=agent.model,
            cwd=agent.cwd,
            api_base=agent.api_base,
            messages_json=self._encode_messages(agent.messages),
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(self._insert_record, record)
        self.sessions[session_id] = agent
        return agent

    async def create_session(self, title: str | None, model: str, cwd: str, api_base: str | None) -> dict:
        session_id = uuid4().hex
        agent = Agent(model=model, cwd=os.path.abspath(cwd), api_base=api_base)
        now = self._utc_now()
        record = SessionRecord(
            id=session_id,
            title=title or "New chat",
            model=agent.model,
            cwd=agent.cwd,
            api_base=agent.api_base,
            messages_json=self._encode_messages(agent.messages),
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(self._insert_record, record)
        self.sessions[session_id] = agent
        return {
            "session": {
                "id": record.id,
                "title": record.title,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            },
            "messages": [],
        }

    async def list_sessions(self) -> list[dict]:
        records = await asyncio.to_thread(self._list_records)
        return [
            {
                "id": record.id,
                "title": record.title,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            for record in records
        ]

    async def load_session(self, session_id: str) -> dict | None:
        record = await asyncio.to_thread(self._load_record, session_id)
        if not record:
            return None
        messages = self._decode_messages(record.messages_json, [])
        return {
            "session": {
                "id": record.id,
                "title": record.title,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            },
            "messages": self._build_ui_messages(messages),
        }

    async def save_session(self, session_id: str, agent: Agent) -> None:
        record = await asyncio.to_thread(self._load_record, session_id)
        if not record:
            return
        updated_title = record.title
        if not updated_title or updated_title == "New chat":
            updated_title = self._infer_title(agent.messages)
        record = SessionRecord(
            id=record.id,
            title=updated_title,
            model=agent.model,
            cwd=agent.cwd,
            api_base=agent.api_base,
            messages_json=self._encode_messages(agent.messages),
            created_at=record.created_at,
            updated_at=self._utc_now(),
        )
        await asyncio.to_thread(self._update_record, record)

    async def clear(self, session_id: str) -> None:
        agent = self.sessions.get(session_id)
        if agent:
            agent.clear()
        record = await asyncio.to_thread(self._load_record, session_id)
        if not record:
            return
        if not agent:
            agent = Agent(model=record.model, cwd=record.cwd, api_base=record.api_base)
        messages = agent.messages
        record = SessionRecord(
            id=record.id,
            title=record.title,
            model=record.model,
            cwd=record.cwd,
            api_base=record.api_base,
            messages_json=self._encode_messages(messages),
            created_at=record.created_at,
            updated_at=self._utc_now(),
        )
        await asyncio.to_thread(self._update_record, record)

    async def delete(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        await asyncio.to_thread(self._delete_record, session_id)

    def get(self, session_id: str) -> Agent | None:
        return self.sessions.get(session_id)
