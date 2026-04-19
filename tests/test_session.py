"""Basic tests for SessionStore (append-only JSONL storage)."""

import tempfile
from pathlib import Path

import pytest

from mycode.session import SessionStore


@pytest.fixture
def temp_store():
    """Provide a SessionStore with a temp data directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SessionStore(data_dir=Path(tmpdir))
        yield store


class TestSessionStore:
    """Tests for SessionStore CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_session(self, temp_store):
        """Session creation should persist metadata immediately."""
        result = await temp_store.create_session("s1", cwd="/home/user/project")

        session = result["session"]
        assert session["id"] == "s1"
        assert session["cwd"] == "/home/user/project"
        assert "created_at" in session
        assert "updated_at" in session
        assert result["messages"] == []

        sessions = await temp_store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, temp_store):
        """Listing sessions with no data should return empty list."""
        sessions = await temp_store.list_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_list_sessions_with_data(self, temp_store):
        """Listing should return sessions sorted by updated_at desc."""
        await temp_store.create_session("first", cwd="/tmp")
        await temp_store.create_session("second", cwd="/tmp")
        await temp_store.append_message(
            "first",
            {"role": "user", "content": [{"type": "text", "text": "first"}]},
        )
        await temp_store.append_message(
            "second",
            {"role": "user", "content": [{"type": "text", "text": "second"}]},
        )

        sessions = await temp_store.list_sessions()
        assert len(sessions) == 2
        # Should be sorted by updated_at descending (newest first)
        assert sessions[0]["updated_at"] >= sessions[1]["updated_at"]

    @pytest.mark.asyncio
    async def test_latest_session_returns_newest_match(self, temp_store):
        """latest_session should return the most recently updated session."""
        await temp_store.create_session("first", cwd="/tmp")
        await temp_store.create_session("second", cwd="/tmp")
        await temp_store.append_message(
            "first",
            {"role": "user", "content": [{"type": "text", "text": "bump first"}]},
        )

        latest = await temp_store.latest_session(cwd="/tmp")
        assert latest is not None
        assert latest["id"] == "first"

    @pytest.mark.asyncio
    async def test_load_session_not_found(self, temp_store):
        """Loading non-existent session should return None."""
        result = await temp_store.load_session("non-existent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_session_with_messages(self, temp_store):
        """Loading session should restore persisted messages."""
        await temp_store.create_session("s1", cwd="/tmp")
        await temp_store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})
        await temp_store.append_message("s1", {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]})

        loaded = await temp_store.load_session("s1")
        assert loaded is not None
        assert len(loaded["messages"]) == 2
        assert loaded["messages"][0]["role"] == "user"
        assert loaded["messages"][0]["content"] == [{"type": "text", "text": "Hello"}]
        assert loaded["messages"][1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_load_session_repairs_interrupted_tool_loop(self, temp_store):
        """Loading should append a synthetic result for an interrupted tool loop."""

        await temp_store.create_session("s1", cwd="/tmp")
        await temp_store.append_message(
            "s1",
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
            },
        )

        loaded = await temp_store.load_session("s1")

        assert loaded is not None
        assert loaded["messages"] == [
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

        loaded_again = await temp_store.load_session("s1")
        assert loaded_again is not None
        assert loaded_again["messages"] == loaded["messages"]

    @pytest.mark.asyncio
    async def test_title_derived_from_first_user_message(self, temp_store):
        """Title is derived at read time from the first user message."""
        await temp_store.create_session("s1", cwd="/tmp")
        await temp_store.append_message(
            "s1",
            {"role": "user", "content": [{"type": "text", "text": "How do I write a Python function?"}]},
        )

        loaded = await temp_store.load_session("s1")
        assert loaded["session"]["title"] == "How do I write a Python function?"

    @pytest.mark.asyncio
    async def test_clear_session(self, temp_store):
        """Clearing session should remove all messages but keep meta."""
        await temp_store.create_session("s1", cwd="/tmp")
        await temp_store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})
        await temp_store.clear_session("s1")

        loaded = await temp_store.load_session("s1")
        assert loaded["messages"] == []
        assert loaded["session"]["cwd"] == "/tmp"  # Meta preserved

    @pytest.mark.asyncio
    async def test_delete_session(self, temp_store):
        """Deleting session should remove all files."""
        await temp_store.create_session("s1", cwd="/tmp")
        await temp_store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "Hello"}]})

        session_dir = temp_store.session_dir("s1")
        assert session_dir.exists()

        await temp_store.delete_session("s1")

        assert not session_dir.exists()
        assert await temp_store.load_session("s1") is None


class TestSessionStoreEdgeCases:
    """Edge case tests for SessionStore."""

    @pytest.mark.asyncio
    async def test_create_session_is_lazy_for_tool_output(self, temp_store):
        """Creating a session should not create a tool-output subdir eagerly."""
        await temp_store.create_session("s1", cwd="/tmp")

        # Session dir and messages.jsonl exist; tool-output belongs to the
        # tool executor and is created lazily only when bash spills.
        assert temp_store.meta_path("s1").exists()
        assert temp_store.messages_path("s1").exists()
        assert not (temp_store.session_dir("s1") / "tool-output").exists()
