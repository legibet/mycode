"""Tests for the CLI session store: catalog (meta.json) plus SDK timeline."""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from mycode.messages import BlockMeta, ConversationMessage, build_message, text_block
from mycode_cli.sessions import SessionLock, SessionLockedError, SessionStore, display_text


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


async def test_lock_admits_one_holder_and_names_it_to_the_others(store: SessionStore, tmp_path: Path) -> None:
    lock = store.lock("s1", "the terminal UI")
    with pytest.raises(SessionLockedError, match=rf"^session s1 is in use by the terminal UI \(pid {os.getpid()}\)$"):
        store.lock("s1", "the web server")

    await store.record_user_turn("s1", cwd=str(tmp_path), text="hi")
    lock.release()
    lock.release()

    with store.lock("s1", "the web server"):
        pass
    # A written session keeps its directory.
    assert [session["id"] for session in await store.list_sessions()] == ["s1"]


def test_releasing_the_lock_of_an_untouched_session_removes_its_directory(store: SessionStore) -> None:
    with store.lock("fresh", "the terminal UI"):
        assert store.session_dir("fresh").is_dir()

    assert not store.session_dir("fresh").exists()


def test_lock_taken_on_a_lock_file_its_holder_removed_is_retried(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = store.lock("fresh", "the web server")
    flock = fcntl.flock
    released = False

    def release_then_flock(handle: Any, operation: int) -> None:
        # The file is open; the holder releases its untouched session before the flock.
        nonlocal released
        if not released:
            released = True
            holder.release()
        flock(handle, operation)

    monkeypatch.setattr(fcntl, "flock", release_then_flock)
    lock = store.lock("fresh", "the terminal UI")
    monkeypatch.setattr(fcntl, "flock", flock)

    with pytest.raises(SessionLockedError, match="in use by the terminal UI"):
        store.lock("fresh", "the web server")
    lock.release()


def test_lock_held_by_another_process_is_dropped_when_it_exits(store: SessionStore) -> None:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from mycode_cli.sessions import SessionStore\n"
        "lock = SessionStore(data_dir=Path(sys.argv[1])).lock('s1', 'mycode run')\n"
        "print('locked', flush=True)\n"
        "sys.stdin.read()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(store.data_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        with pytest.raises(SessionLockedError, match=rf"in use by mycode run \(pid {holder.pid}\)"):
            store.lock("s1", "the terminal UI")
    finally:
        # A killed holder never releases; the kernel drops its lock.
        holder.kill()
        holder.wait()

    with store.lock("s1", "the terminal UI"):
        pass


async def test_session_lifecycle_preserves_metadata_and_messages(store: SessionStore, tmp_path: Path) -> None:
    assert await store.list_sessions() == []
    assert await store.load_session("s1") is None
    cwd = str(tmp_path.resolve())

    session = await store.create_session("s1", cwd=cwd)

    assert session["id"] == "s1"
    assert session["cwd"] == cwd
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
    await store.record_user_turn("s1", cwd=cwd, text="How do I write a Python function?")

    loaded = await store.load_session("s1")
    assert loaded is not None
    assert loaded["messages"] == messages
    assert loaded["session"]["title"] == "How do I write a Python function?"

    await store.delete_session("s1")
    assert await store.load_session("s1") is None
    assert await store.list_sessions() == []


async def test_record_user_turn_creates_catalog_entry_and_keeps_title(store: SessionStore, tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    first = await store.record_user_turn("s1", cwd=cwd, text="first question\nwith newline")

    assert first["title"] == "first question with newline"
    assert first["cwd"] == cwd

    second = await store.record_user_turn("s1", cwd=cwd, text="second question")

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


async def test_session_listing_matches_a_workspace_reached_through_a_symlink(
    store: SessionStore, tmp_path: Path
) -> None:
    real = tmp_path / "project"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    await store.create_session("via-link", cwd=str(link))
    await store.create_session("via-real", cwd=str(real))

    for cwd in (link, real):
        assert {session["id"] for session in await store.list_sessions(cwd=str(cwd))} == {"via-link", "via-real"}


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


async def test_delete_session_under_its_lock_removes_the_whole_directory(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")
    await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "hi"}]})
    (store.session_dir("s1") / "tool-output").mkdir()

    with store.lock("s1", "the web server"):
        await store.delete_session("s1")

    assert not store.session_dir("s1").exists()


def test_a_holder_arriving_while_a_delete_removes_the_lock_file_keeps_its_session(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asyncio.run(store.record_user_turn("s1", cwd=str(tmp_path), text="old"))
    rmdir = Path.rmdir
    arrived: list[SessionLock] = []

    def arrive_then_rmdir(path: Path) -> None:
        # The deleting holder has removed the lock file but not yet the directory.
        lock = store.lock("s1", "the terminal UI")
        arrived.append(lock)
        asyncio.run(store.record_user_turn("s1", cwd=str(tmp_path), text="new"))
        rmdir(path)

    monkeypatch.setattr(Path, "rmdir", arrive_then_rmdir)
    with store.lock("s1", "the web server"):
        asyncio.run(store.delete_session("s1"))
    monkeypatch.setattr(Path, "rmdir", rmdir)

    [lock] = arrived
    with pytest.raises(SessionLockedError, match="in use by the terminal UI"):
        store.lock("s1", "the web server")
    session = asyncio.run(store.load_metadata("s1"))
    assert session is not None
    assert session["title"] == "new"
    lock.release()


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


def test_display_text_skips_payload_blocks() -> None:
    job: BlockMeta = {"job": {"tool_use_id": "toolu_1", "name": "bash", "label": "pytest -q", "exit_code": 0}}
    message = build_message(
        "user",
        [
            text_block("Background bash finished", meta=job),
            text_block("skill", meta={"skill_snapshot": True}),
            text_block("<file>…</file>", meta={"attachment": True}),
            text_block("keep this"),
        ],
    )
    assert display_text(message) == "keep this"


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
