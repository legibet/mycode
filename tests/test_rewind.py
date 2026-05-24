"""Tests for conversation rewind behavior."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mycode.compact import build_compact_event
from mycode.messages import ConversationMessage
from mycode.session import SessionStore
from mycode_cli.server.app import create_app
from mycode_cli.server.deps import get_run_manager, get_store
from mycode_cli.server.run_manager import RunManager


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


def text_message(role: str, text: str) -> ConversationMessage:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def message_texts(messages: list[ConversationMessage]) -> list[str]:
    return [str((message.get("content") or [{}])[0].get("text")) for message in messages]


async def append_messages(
    store: SessionStore,
    session_id: str,
    messages: list[ConversationMessage],
) -> None:
    for message in messages:
        await store.append_message(session_id, message)


@pytest.mark.asyncio
async def test_rewind_replaces_the_visible_tail_without_rewriting_the_log(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await append_messages(
        store,
        "s1",
        [
            text_message("user", "hello"),
            text_message("assistant", "hi"),
            text_message("user", "explain X"),
            text_message("assistant", "X is..."),
        ],
    )

    await store.append_rewind("s1", 2)
    await store.append_message("s1", text_message("user", "explain Y instead"))

    loaded = await store.load_session("s1")
    raw_lines = store.messages_path("s1").read_text(encoding="utf-8").strip().splitlines()

    assert loaded is not None
    assert message_texts(loaded["messages"]) == ["hello", "hi", "explain Y instead"]
    assert len(raw_lines) == 6


@pytest.mark.asyncio
async def test_rewind_can_discard_the_entire_visible_history(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", text_message("user", "old start"))

    await store.append_rewind("s1", 0)
    await store.append_message("s1", text_message("user", "fresh start"))

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert message_texts(loaded["messages"]) == ["fresh start"]


@pytest.mark.asyncio
async def test_multiple_rewinds_are_applied_in_order(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await append_messages(store, "s1", [text_message("user", "a"), text_message("assistant", "b")])

    await store.append_message("s1", text_message("user", "c"))
    await store.append_rewind("s1", 2)
    await store.append_message("s1", text_message("user", "d"))
    await store.append_rewind("s1", 2)
    await store.append_message("s1", text_message("user", "e"))

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert message_texts(loaded["messages"]) == ["a", "b", "e"]


@pytest.mark.asyncio
async def test_rewind_before_a_compact_marker_drops_the_marker(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await append_messages(
        store,
        "s1",
        [
            text_message("user", "hello"),
            text_message("assistant", "hi"),
            build_compact_event("summary", provider="p", model="m"),
            text_message("user", "explain X"),
        ],
    )

    await store.append_rewind("s1", 0)
    await store.append_message("s1", text_message("user", "fresh start"))

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert [message["role"] for message in loaded["messages"]] == ["user"]
    assert message_texts(loaded["messages"]) == ["fresh start"]


@pytest.mark.asyncio
async def test_rewind_after_a_compact_marker_keeps_the_marker(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await append_messages(
        store,
        "s1",
        [
            text_message("user", "hello"),
            text_message("assistant", "hi"),
            build_compact_event("summary", provider="p", model="m"),
            text_message("user", "explain X"),
            text_message("assistant", "X is..."),
        ],
    )

    await store.append_rewind("s1", 3)
    await store.append_message("s1", text_message("user", "explain Y instead"))

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert [message["role"] for message in loaded["messages"]] == ["user", "assistant", "compact", "user"]
    assert message_texts(loaded["messages"]) == ["hello", "hi", "summary", "explain Y instead"]


@pytest.mark.asyncio
async def test_rewind_removes_an_interrupted_tool_call_from_visible_history(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await append_messages(
        store,
        "s1",
        [
            text_message("user", "hello"),
            text_message("assistant", "hi"),
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
            },
        ],
    )

    await store.append_rewind("s1", 2)

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert message_texts(loaded["messages"]) == ["hello", "hi"]


def test_chat_rejects_rewind_to_compact_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    store = SessionStore(data_dir=tmp_path / "sessions")
    asyncio.run(store.create_session("chat-42", cwd="/tmp"))
    sid = "chat-42"

    for message in [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("summary", provider="p", model="m"),
        {"role": "user", "content": [{"type": "text", "text": "explain X"}]},
    ]:
        asyncio.run(store.append_message(sid, message))

    app = create_app(serve_web=False)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: RunManager()

    with TestClient(app) as client:
        # Visible list: [user, assistant, compact, user_X]. Rewinding to the
        # compact marker (index 2) is invalid — it is not a real user message.
        response = client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "message": "retry",
                "rewind_to": 2,
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "cwd": "/tmp",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "rewind_to must reference a real user message"


def test_chat_rejects_rewind_for_new_session_without_creating_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    store = SessionStore(data_dir=tmp_path / "sessions")
    app = create_app(serve_web=False)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: RunManager()

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": "new-session",
                "message": "retry",
                "rewind_to": 0,
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "cwd": "/tmp",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "rewind_to requires an existing session"
    assert not store.session_dir("new-session").exists()
