"""Tests for the CLI session store: catalog (meta.json) plus SDK timeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode_cli.sessions import SessionStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


async def test_session_lifecycle_preserves_metadata_and_messages(store: SessionStore) -> None:
    assert await store.list_sessions() == []
    assert await store.load_session("s1") is None

    session = await store.create_session("s1", cwd="/home/user/project")

    assert session["id"] == "s1"
    assert session["cwd"] == "/home/user/project"
    assert session["title"] == "New chat"
    assert "created_at" in session
    assert "updated_at" in session
    assert [item["id"] for item in await store.list_sessions()] == ["s1"]

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "How do I write a Python function?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Start with def."}]},
    ]
    for message in messages:
        await store.append_message("s1", message)
    await store.record_user_turn("s1", cwd="/home/user/project", text="How do I write a Python function?")

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


async def test_record_user_turn_creates_catalog_entry_and_keeps_title(store: SessionStore) -> None:
    first = await store.record_user_turn("s1", cwd="/tmp", text="first question\nwith newline")

    assert first["title"] == "first question with newline"
    assert first["cwd"] == "/tmp"

    second = await store.record_user_turn("s1", cwd="/tmp", text="second question")

    assert second["title"] == "first question with newline"
    assert str(second["updated_at"]) >= str(first["updated_at"])


async def test_record_user_turn_without_text_keeps_default_title(store: SessionStore) -> None:
    created = await store.record_user_turn("s1", cwd="/tmp", text="")
    assert created["title"] == "New chat"

    promoted = await store.record_user_turn("s1", cwd="/tmp", text="real question")
    assert promoted["title"] == "real question"


async def test_session_listing_filters_and_orders_by_recent_activity(store: SessionStore, tmp_path: Path) -> None:
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")
    await store.create_session("first", cwd=current_cwd)
    await store.create_session("second", cwd=current_cwd)
    await store.create_session("other", cwd=other_cwd)
    await store.touch("first")

    current = await store.list_sessions(cwd=current_cwd)
    latest = await store.latest_session(cwd=current_cwd)

    assert [session["id"] for session in current] == ["first", "second"]
    assert latest is not None
    assert latest["id"] == "first"
    assert {session["id"] for session in await store.list_sessions()} == {"first", "second", "other"}


async def test_listing_ignores_sessions_without_a_catalog_entry(store: SessionStore) -> None:
    # An SDK-only session (timeline without meta.json) is invisible to the catalog.
    await store.append_message("bare", {"role": "user", "content": [{"type": "text", "text": "hi"}]})

    assert await store.list_sessions() == []
    assert await store.load_session("bare") is None


async def test_delete_session_removes_the_whole_directory(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "hi"}]})
    (store.session_dir("s1") / "tool-output").mkdir()

    await store.delete_session("s1")

    assert not store.session_dir("s1").exists()
