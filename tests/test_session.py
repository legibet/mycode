"""Tests for SessionStore append-only storage."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from mycode.session import SessionStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


async def test_session_lifecycle_preserves_metadata_and_messages(store: SessionStore) -> None:
    assert await store.list_sessions() == []
    assert await store.load_session("s1") is None

    result = await store.create_session("s1", cwd="/home/user/project")

    session = result["session"]
    assert session["id"] == "s1"
    assert session["cwd"] == "/home/user/project"
    assert "created_at" in session
    assert "updated_at" in session
    assert result["messages"] == []
    assert [item["id"] for item in await store.list_sessions()] == ["s1"]

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "How do I write a Python function?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Start with def."}]},
    ]
    for message in messages:
        await store.append_message("s1", message)

    loaded = await store.load_session("s1")
    assert loaded is not None
    assert loaded["messages"] == messages
    assert loaded["session"]["title"] == "How do I write a Python function?"

    await store.clear_session("s1")

    cleared = await store.load_session("s1")
    assert cleared is not None
    assert cleared["messages"] == []
    assert cleared["session"]["cwd"] == "/home/user/project"
    assert cleared["session"]["title"] == "New chat"

    await store.delete_session("s1")
    assert await store.load_session("s1") is None
    assert await store.list_sessions() == []


async def test_load_raw_messages_keeps_rewound_tails(store: SessionStore) -> None:
    assert await store.load_raw_messages("s1") == []

    await store.create_session("s1", cwd="/tmp")
    records = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "rewound reply"}]},
    ]
    for record in records:
        await store.append_message("s1", record)
    await store.append_rewind("s1", 0)

    loaded = await store.load_session("s1")
    assert loaded is not None
    assert loaded["messages"] == []

    raw = await store.load_raw_messages("s1")
    assert [record.get("role") for record in raw] == ["user", "assistant", "rewind"]


async def test_session_listing_filters_and_orders_by_recent_activity(store: SessionStore, tmp_path: Path) -> None:
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")
    await store.create_session("first", cwd=current_cwd)
    await store.create_session("second", cwd=current_cwd)
    await store.create_session("other", cwd=other_cwd)
    await store.append_message("first", {"role": "user", "content": [{"type": "text", "text": "bump first"}]})

    current = await store.list_sessions(cwd=current_cwd)
    latest = await store.latest_session(cwd=current_cwd)

    assert [session["id"] for session in current] == ["first", "second"]
    assert latest is not None
    assert latest["id"] == "first"
    assert {session["id"] for session in await store.list_sessions()} == {"first", "second", "other"}


@pytest.mark.parametrize("index_state", ["missing", "damaged"])
async def test_list_sessions_recovers_when_index_is_unavailable(
    store: SessionStore,
    index_state: Literal["missing", "damaged"],
) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})
    if index_state == "missing":
        store.index_path().unlink()
    else:
        store.index_path().write_text("{bad json", encoding="utf-8")

    sessions = await store.list_sessions(cwd="/tmp")

    assert [(session["id"], session["title"]) for session in sessions] == [("s1", "Hello")]


async def test_load_session_preserves_orphan_tool_use(store: SessionStore) -> None:
    """SessionStore is a pure reader; closing orphan tool_use blocks is the provider's job."""

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
    ]

    assert loaded is not None
    assert loaded["messages"] == expected_messages
    assert loaded_again is not None
    assert loaded_again["messages"] == expected_messages
