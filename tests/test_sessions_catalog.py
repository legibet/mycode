"""Tests for the CLI session store: catalog (meta.json) plus SDK timeline."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from mycode.messages import ConversationMessage
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

    messages: list[ConversationMessage] = [
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


async def test_listing_skips_sessions_without_a_valid_catalog_entry(store: SessionStore) -> None:
    await store.create_session("ok", cwd="/tmp")
    # An SDK-only session (timeline without meta.json) is invisible to the catalog.
    await store.append_message("bare", {"role": "user", "content": [{"type": "text", "text": "hi"}]})
    null_field = '{"cwd": "/tmp", "title": "t", "created_at": "2026-01-01", "updated_at": null}'
    for session_id, meta in [("broken", "{"), ("null-field", null_field)]:
        store.meta_path(session_id).parent.mkdir(parents=True)
        store.meta_path(session_id).write_text(meta)

    assert [session["id"] for session in await store.list_sessions()] == ["ok"]
    assert await store.load_session("bare") is None
    assert await store.load_session("null-field") is None


async def test_delete_session_removes_the_whole_directory(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "hi"}]})
    (store.session_dir("s1") / "tool-output").mkdir()

    await store.delete_session("s1")

    assert not store.session_dir("s1").exists()


async def test_load_totals_folds_the_raw_timeline_including_rewound_turns(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    records: list[ConversationMessage] = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [],
            "meta": {
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 100, "cache_read_tokens": 80, "output_tokens": 10},
                "cost": {"input": 0.01, "output": 0.01, "total": 0.02},
            },
        },
        {
            "role": "compact",
            "content": [],
            "meta": {
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 50, "output_tokens": 5},
                "cost": {"input": 0.003, "output": 0.002, "total": 0.005},
            },
        },
    ]
    for record in records:
        await store.append_message("s1", record)
    await store.append_rewind("s1", 0)

    totals = await store.load_totals("s1")
    assert totals.usage == {"input_tokens": 150, "cache_read_tokens": 80, "output_tokens": 15}
    assert totals.cost == pytest.approx({"input": 0.013, "output": 0.012, "total": 0.025})


async def test_load_totals_skips_records_without_usage_or_cost(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message(
        "s1",
        {
            "role": "assistant",
            "content": [],
            "meta": {"provider": "p", "model": "m", "cost": {"total": 0.02}},
        },
    )
    # A cancelled stream without cost must not hide known session costs.
    await store.append_message("s1", {"role": "assistant", "content": [], "meta": {"provider": "p", "model": "m"}})

    totals = await store.load_totals("s1")
    assert totals.usage == {}
    assert totals.cost == pytest.approx({"total": 0.02})

    await store.create_session("s2", cwd="/tmp")
    await store.append_message(
        "s2",
        {
            "role": "assistant",
            "content": [],
            "meta": {"provider": "unknown", "model": "unknown", "usage": {"input_tokens": 10, "output_tokens": 5}},
        },
    )
    totals = await store.load_totals("s2")
    assert totals.usage == {"input_tokens": 10, "output_tokens": 5}
    assert totals.cost is None


# Search


def _text(role: Literal["user", "assistant"], text: str) -> ConversationMessage:
    return {"role": role, "content": [{"type": "text", "text": text}]}


async def test_search_matches_title_without_snippet(store: SessionStore) -> None:
    await store.record_user_turn("s1", cwd="/tmp", text="Refactor the Parser")
    await store.append_message("s1", _text("user", "unrelated body"))

    hits = await store.search_sessions("parser")

    assert [hit["session"]["id"] for hit in hits] == ["s1"]
    assert hits[0]["snippet"] is None


async def test_search_body_snippet_keeps_original_casing_and_collapses_whitespace(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", _text("user", "first question"))
    before = "x" * 70
    await store.append_message("s1", _text("assistant", f"{before}\n\n  the  TokenBucket\nclass " + "y" * 100))
    await store.append_message("s1", _text("user", "tokenbucket again"))

    hits = await store.search_sessions("tokenbucket")

    assert len(hits) == 1
    assert hits[0]["snippet"] == {
        "before": ("x" * 70 + " the ")[-60:],
        "match": "TokenBucket",
        "after": (" class " + "y" * 100)[:80],
    }


async def test_search_skips_rewound_turns_and_skill_snapshots(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", _text("user", "kept"))
    await store.append_message("s1", _text("user", "rewound needle"))
    await store.append_rewind("s1", 1)
    await store.append_message(
        "s1",
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "skill needle", "meta": {"skill_snapshot": True}},
                {"type": "text", "text": "visible"},
            ],
        },
    )

    assert await store.search_sessions("needle") == []
    assert len(await store.search_sessions("VISIBLE")) == 1


async def test_search_applies_cwd_filter_limit_and_blank_query(store: SessionStore, tmp_path: Path) -> None:
    project = str(tmp_path / "a")
    for session_id, cwd in [("a1", project), ("a2", project), ("b1", str(tmp_path / "b"))]:
        await store.record_user_turn(session_id, cwd=cwd, text="shared topic")

    assert {hit["session"]["id"] for hit in await store.search_sessions("topic", cwd=project)} == {"a1", "a2"}
    assert len(await store.search_sessions("topic", limit=2)) == 2
    assert await store.search_sessions("   \n") == []


async def test_search_reflects_appends_and_rewinds_after_earlier_search(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", _text("user", "alpha"))
    assert await store.search_sessions("beta") == []

    await store.append_message("s1", _text("assistant", "beta"))
    assert len(await store.search_sessions("beta")) == 1

    await store.append_rewind("s1", 1)
    assert await store.search_sessions("beta") == []
    assert len(await store.search_sessions("alpha")) == 1
