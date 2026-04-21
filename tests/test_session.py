"""Tests for SessionStore append-only storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode.session import SessionStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


async def test_create_session_persists_metadata(store: SessionStore) -> None:
    result = await store.create_session("s1", cwd="/home/user/project")

    session = result["session"]
    assert session["id"] == "s1"
    assert session["cwd"] == "/home/user/project"
    assert "created_at" in session
    assert "updated_at" in session
    assert result["messages"] == []
    assert [item["id"] for item in await store.list_sessions()] == ["s1"]


async def test_list_sessions_empty(store: SessionStore) -> None:
    assert await store.list_sessions() == []


async def test_list_sessions_returns_newest_first(store: SessionStore) -> None:
    await store.create_session("first", cwd="/tmp")
    await store.create_session("second", cwd="/tmp")
    await store.append_message("first", {"role": "user", "content": [{"type": "text", "text": "first"}]})
    await store.append_message("second", {"role": "user", "content": [{"type": "text", "text": "second"}]})

    sessions = await store.list_sessions()

    assert len(sessions) == 2
    assert str(sessions[0]["updated_at"]) >= str(sessions[1]["updated_at"])


async def test_list_sessions_filters_by_workspace(store: SessionStore, tmp_path: Path) -> None:
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")
    await store.create_session("current", cwd=current_cwd)
    await store.create_session("other", cwd=other_cwd)

    current = await store.list_sessions(cwd=current_cwd)
    all_sessions = await store.list_sessions(cwd=None)

    assert [session["id"] for session in current] == ["current"]
    assert {session["id"] for session in all_sessions} == {"current", "other"}


async def test_latest_session_returns_most_recent_match(store: SessionStore) -> None:
    await store.create_session("first", cwd="/tmp")
    await store.create_session("second", cwd="/tmp")
    await store.append_message("first", {"role": "user", "content": [{"type": "text", "text": "bump first"}]})

    latest = await store.latest_session(cwd="/tmp")

    assert latest is not None
    assert latest["id"] == "first"


async def test_load_session_returns_none_for_missing_session(store: SessionStore) -> None:
    assert await store.load_session("missing") is None


async def test_load_session_restores_persisted_messages(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})
    await store.append_message("s1", {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]})

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
    ]


async def test_load_session_repairs_interrupted_tool_loop(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message(
        "s1",
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
        },
    )

    loaded = await store.load_session("s1")
    loaded_again = await store.load_session("s1")

    expected_messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "output": "error: tool call was interrupted",
                    "is_error": True,
                }
            ],
        },
    ]

    assert loaded is not None
    assert loaded["messages"] == expected_messages
    assert loaded_again is not None
    assert loaded_again["messages"] == expected_messages


async def test_load_session_derives_title_from_first_user_message(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message(
        "s1",
        {"role": "user", "content": [{"type": "text", "text": "How do I write a Python function?"}]},
    )

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["session"]["title"] == "How do I write a Python function?"


async def test_clear_session_keeps_metadata(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})

    await store.clear_session("s1")
    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"] == []
    assert loaded["session"]["cwd"] == "/tmp"


async def test_delete_session_removes_all_files(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})

    session_dir = store.session_dir("s1")
    assert session_dir.exists()

    await store.delete_session("s1")

    assert not session_dir.exists()
    assert await store.load_session("s1") is None


async def test_create_session_does_not_eagerly_create_tool_output_dir(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")

    assert store.meta_path("s1").exists()
    assert store.messages_path("s1").exists()
    assert not (store.session_dir("s1") / "tool-output").exists()
