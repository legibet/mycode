"""Session storage with SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.core import Agent


@dataclass
class SessionRecord:
    """Database record for a chat session."""

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
    """In-memory session cache with SQLite persistence."""

    sessions: dict[str, Agent] = field(default_factory=dict)
    db_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "sessions.db")

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
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
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, session_id: str) -> Agent | None:
        """Get agent from memory cache."""
        return self.sessions.get(session_id)

    async def get_or_create(self, session_id: str, model: str, cwd: str, api_base: str | None) -> Agent:
        """Get existing agent or create new one."""
        cwd = os.path.abspath(cwd)

        # Check memory cache
        agent = self.sessions.get(session_id)
        if agent:
            # Recreate if config changed
            if agent.model != model or agent.cwd != cwd or agent.api_base != api_base:
                agent = Agent(model=model, cwd=cwd, api_base=api_base)
                self.sessions[session_id] = agent
            return agent

        # Try load from database
        def load() -> SessionRecord | None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                return SessionRecord(**dict(row)) if row else None

        record = await asyncio.to_thread(load)
        if record:
            agent = Agent(model=model, cwd=cwd, api_base=api_base)
            try:
                agent.messages = json.loads(record.messages_json)
            except json.JSONDecodeError:
                pass
            self.sessions[session_id] = agent
            return agent

        # Create new session
        agent = Agent(model=model, cwd=cwd, api_base=api_base)
        now = datetime.now(UTC).isoformat()

        def insert() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO sessions (id, title, model, cwd, api_base, messages_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, "New chat", model, cwd, api_base, json.dumps(agent.messages), now, now),
                )
                conn.commit()

        await asyncio.to_thread(insert)
        self.sessions[session_id] = agent
        return agent

    async def create_session(self, title: str | None, model: str, cwd: str, api_base: str | None) -> dict:
        """Create a new chat session."""
        session_id = uuid4().hex
        cwd = os.path.abspath(cwd)
        agent = Agent(model=model, cwd=cwd, api_base=api_base)
        now = datetime.now(UTC).isoformat()

        def insert() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO sessions (id, title, model, cwd, api_base, messages_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, title or "New chat", model, cwd, api_base, json.dumps(agent.messages), now, now),
                )
                conn.commit()

        await asyncio.to_thread(insert)
        self.sessions[session_id] = agent
        return {
            "session": {"id": session_id, "title": title or "New chat", "created_at": now, "updated_at": now},
            "messages": [],
        }

    async def list_sessions(self, cwd: str | None = None) -> list[dict]:
        """List all sessions, optionally filtered by cwd."""
        normalized_cwd = os.path.abspath(cwd) if cwd else None

        def query() -> list[dict]:
            with self._connect() as conn:
                if normalized_cwd:
                    rows = conn.execute(
                        "SELECT id, title, created_at, updated_at FROM sessions WHERE cwd = ? ORDER BY updated_at DESC",
                        (normalized_cwd,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
                    ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(query)

    async def load_session(self, session_id: str) -> dict | None:
        """Load session with raw messages (UI formatting done in frontend)."""

        def load() -> dict | None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if not row:
                    return None
                record = dict(row)
                try:
                    messages = json.loads(record["messages_json"])
                except json.JSONDecodeError:
                    messages = []
                return {
                    "session": {
                        "id": record["id"],
                        "title": record["title"],
                        "model": record["model"],
                        "cwd": record["cwd"],
                        "api_base": record["api_base"],
                        "created_at": record["created_at"],
                        "updated_at": record["updated_at"],
                    },
                    "messages": messages,  # Raw provider format, let frontend transform
                }

        return await asyncio.to_thread(load)

    async def save_session(self, session_id: str, agent: Agent) -> None:
        """Save agent state to database."""

        def save() -> None:
            with self._connect() as conn:
                row = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if not row:
                    return
                title = row["title"]
                # Infer title from first user message if still default
                if title == "New chat":
                    for msg in agent.messages:
                        if msg.get("role") == "user":
                            content = (msg.get("content") or "").strip().replace("\n", " ")
                            if content:
                                title = content[:48]
                                break
                conn.execute(
                    "UPDATE sessions SET title = ?, model = ?, cwd = ?, api_base = ?, messages_json = ?, updated_at = ? WHERE id = ?",
                    (
                        title,
                        agent.model,
                        agent.cwd,
                        agent.api_base,
                        json.dumps(agent.messages),
                        datetime.now(UTC).isoformat(),
                        session_id,
                    ),
                )
                conn.commit()

        await asyncio.to_thread(save)

    async def clear(self, session_id: str) -> None:
        """Clear session messages."""
        agent = self.sessions.get(session_id)
        if agent:
            agent.clear()

        def update() -> None:
            with self._connect() as conn:
                row = conn.execute("SELECT model, cwd, api_base FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if not row:
                    return
                temp_agent = agent or Agent(model=row["model"], cwd=row["cwd"], api_base=row["api_base"])
                conn.execute(
                    "UPDATE sessions SET messages_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(temp_agent.messages), datetime.now(UTC).isoformat(), session_id),
                )
                conn.commit()

        await asyncio.to_thread(update)

    async def delete(self, session_id: str) -> None:
        """Delete session."""
        self.sessions.pop(session_id, None)

        def delete() -> None:
            with self._connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()

        await asyncio.to_thread(delete)
