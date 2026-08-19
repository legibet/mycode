"""Tests for the SDK's append-only message timeline store."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode.session import SessionStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


async def test_append_creates_session_and_replays_messages(store: SessionStore) -> None:
    assert not store.session_exists("s1")
    assert await store.load_messages("s1") == []

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "How do I write a Python function?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Start with def."}]},
    ]
    for message in messages:
        await store.append_message("s1", message)

    assert store.session_exists("s1")
    assert await store.load_messages("s1") == messages

    await store.clear_messages("s1")
    assert await store.load_messages("s1") == []
    assert store.session_exists("s1")


async def test_load_raw_messages_keeps_rewound_tails(store: SessionStore) -> None:
    assert await store.load_raw_messages("s1") == []

    records = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "rewound reply"}]},
    ]
    for record in records:
        await store.append_message("s1", record)
    await store.append_rewind("s1", 0)

    assert await store.load_messages("s1") == []

    raw = await store.load_raw_messages("s1")
    assert [record.get("role") for record in raw] == ["user", "assistant", "rewind"]


async def test_load_messages_preserves_orphan_tool_use(store: SessionStore) -> None:
    """The store is a pure reader; closing orphan tool_use blocks is the provider's job."""

    message = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
    }
    await store.append_message("s1", message)

    assert await store.load_messages("s1") == [message]
