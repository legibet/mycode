"""Tests for conversation rewind (append-only truncation)."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mycode.core.session import SessionStore, apply_rewind, build_compact_event, build_rewind_event
from mycode.server.app import create_app
from mycode.server.deps import get_run_manager, get_store
from mycode.server.run_manager import RunManager


@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SessionStore(data_dir=Path(tmpdir))


# -- Unit tests for rewind primitives --


class TestBuildRewindEvent:
    def test_basic_structure(self):
        event = build_rewind_event(3)
        assert event["role"] == "rewind"
        assert event["meta"]["rewind_to"] == 3
        assert "created_at" in event["meta"]

    def test_rewind_to_zero(self):
        event = build_rewind_event(0)
        assert event["meta"]["rewind_to"] == 0


class TestApplyRewind:
    def test_no_rewind_events(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        assert apply_rewind(messages) == messages

    def test_single_rewind(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": [{"type": "text", "text": "explain X"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "X is..."}]},
            build_rewind_event(2),
            {"role": "user", "content": [{"type": "text", "text": "explain Y"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Y is..."}]},
        ]
        result = apply_rewind(messages)
        assert len(result) == 4
        assert result[0]["content"][0]["text"] == "hello"
        assert result[1]["content"][0]["text"] == "hi"
        assert result[2]["content"][0]["text"] == "explain Y"
        assert result[3]["content"][0]["text"] == "Y is..."

    def test_multiple_rewinds(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
            {"role": "user", "content": [{"type": "text", "text": "c"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "d"}]},
            build_rewind_event(2),
            {"role": "user", "content": [{"type": "text", "text": "e"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "f"}]},
            build_rewind_event(2),
            {"role": "user", "content": [{"type": "text", "text": "g"}]},
        ]
        result = apply_rewind(messages)
        assert len(result) == 3
        assert result[0]["content"][0]["text"] == "a"
        assert result[1]["content"][0]["text"] == "b"
        assert result[2]["content"][0]["text"] == "g"

    def test_rewind_to_zero(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            build_rewind_event(0),
            {"role": "user", "content": [{"type": "text", "text": "fresh"}]},
        ]
        result = apply_rewind(messages)
        assert len(result) == 1
        assert result[0]["content"][0]["text"] == "fresh"

    def test_rewind_past_compact_discards_compact(self):
        """A rewind that goes before a compact event should discard it."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
            build_compact_event("summary", provider="p", model="m", compacted_count=2),
            build_rewind_event(0),
            {"role": "user", "content": [{"type": "text", "text": "new start"}]},
        ]
        result = apply_rewind(messages)
        assert len(result) == 1
        assert result[0]["content"][0]["text"] == "new start"

    def test_empty_messages(self):
        assert apply_rewind([]) == []


# -- Integration tests with SessionStore --


class TestSessionRewind:
    @pytest.mark.asyncio
    async def test_append_rewind_and_reload(self, temp_store):
        """Rewind event should truncate messages when session is reloaded."""
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": [{"type": "text", "text": "explain X"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "X is..."}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        await temp_store.append_rewind(sid, 2)

        await temp_store.append_message(
            sid,
            {"role": "user", "content": [{"type": "text", "text": "explain Y instead"}]},
            provider="p",
            model="m",
            cwd="/tmp",
            api_base=None,
        )

        loaded = await temp_store.load_session(sid)
        messages = loaded["messages"]
        assert len(messages) == 3
        assert messages[0]["content"][0]["text"] == "hello"
        assert messages[1]["content"][0]["text"] == "hi"
        assert messages[2]["content"][0]["text"] == "explain Y instead"

    @pytest.mark.asyncio
    async def test_rewind_preserves_original_lines_in_jsonl(self, temp_store):
        """JSONL file should contain all original messages plus the rewind marker."""
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        await temp_store.append_rewind(sid, 0)

        raw_lines = temp_store.messages_path(sid).read_text().strip().splitlines()
        assert len(raw_lines) == 3
        assert json.loads(raw_lines[2])["role"] == "rewind"
        assert json.loads(raw_lines[2])["meta"]["rewind_to"] == 0

    @pytest.mark.asyncio
    async def test_rewind_then_compact_works(self, temp_store):
        """Rewind followed by compact should work correctly."""
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
            {"role": "user", "content": [{"type": "text", "text": "c"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "d"}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        # Rewind to keep first 2 messages
        await temp_store.append_rewind(sid, 2)

        # Add new conversation after rewind
        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "e"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "f"}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        # Compact should operate on the post-rewind messages
        compact = build_compact_event("summary of a+b+e+f", provider="p", model="m", compacted_count=4)
        await temp_store.append_message(sid, compact, provider="p", model="m", cwd="/tmp", api_base=None)

        loaded = await temp_store.load_session(sid)
        messages = loaded["messages"]
        # After compact: summary_user + summary_ack (no messages after the compact event)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert "summary of a+b+e+f" in messages[0]["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_rewind_nonexistent_session_is_noop(self, temp_store):
        """Rewinding a session that doesn't exist should not crash."""
        await temp_store.append_rewind("nonexistent", 0)

    @pytest.mark.asyncio
    async def test_rewind_with_interrupted_tool_loop(self, temp_store):
        """Rewind past an interrupted tool loop should not trigger repair."""
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
            },
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        # Rewind to before the tool_use message — the interrupted loop is gone
        await temp_store.append_rewind(sid, 2)

        loaded = await temp_store.load_session(sid)
        messages = loaded["messages"]
        assert len(messages) == 2
        assert messages[0]["content"][0]["text"] == "hello"
        assert messages[1]["content"][0]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_rewind_after_compact_preserves_summary(self, temp_store):
        """Rewind on a compacted session should use post-compact indices,
        preserving the summary rather than falling back to raw JSONL indices."""
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        # Original conversation + compact
        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            build_compact_event("summary of hello+hi", provider="p", model="m", compacted_count=2),
            {"role": "user", "content": [{"type": "text", "text": "explain X"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "X is..."}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        # After loading, messages are:
        # [0] summary_user, [1] summary_ack, [2] user "explain X", [3] asst "X is..."
        loaded = await temp_store.load_session(sid)
        assert len(loaded["messages"]) == 4

        # Rewind to index 2 — keep summary but remove the "explain X" turn.
        await temp_store.append_rewind(sid, 2)
        await temp_store.append_message(
            sid,
            {"role": "user", "content": [{"type": "text", "text": "explain Y instead"}]},
            provider="p",
            model="m",
            cwd="/tmp",
            api_base=None,
        )

        reloaded = await temp_store.load_session(sid)
        messages = reloaded["messages"]

        # Summary must still be present (not lost to raw JSONL index confusion).
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert "summary of hello+hi" in messages[0]["content"][0]["text"]
        assert messages[0]["meta"]["synthetic"] is True  # marked as synthetic
        assert messages[1]["role"] == "assistant"  # summary ack
        assert messages[2]["content"][0]["text"] == "explain Y instead"


def test_chat_rejects_rewind_to_compact_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    store = SessionStore(data_dir=tmp_path / "sessions")
    session = asyncio.run(store.create_session(title="Test", model="m", cwd="/tmp", api_base=None))
    sid = str(session["session"]["id"])

    for message in [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("summary of hello+hi", provider="p", model="m", compacted_count=2),
        {"role": "user", "content": [{"type": "text", "text": "explain X"}]},
    ]:
        asyncio.run(
            store.append_message(
                sid,
                message,
                provider="p",
                model="m",
                cwd="/tmp",
                api_base=None,
            )
        )

    app = create_app(serve_web=False)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: RunManager()

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "message": "retry",
                "rewind_to": 0,
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
