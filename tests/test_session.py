"""Basic tests for SessionStore (append-only JSONL storage)."""

import json
import tempfile
from pathlib import Path

import pytest

from mycode.core.session import SessionStore


@pytest.fixture
def temp_store():
    """Provide a SessionStore with a temp data directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SessionStore(data_dir=Path(tmpdir))
        yield store


def test_default_store_uses_mycode_home(tmp_path, monkeypatch):
    mycode_home = tmp_path / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(mycode_home))

    store = SessionStore()

    assert store.data_dir == (mycode_home / "sessions").resolve()
    assert store.data_dir.exists()


class TestSessionStore:
    """Tests for SessionStore CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_session(self, temp_store):
        """Session creation should persist metadata immediately."""
        result = await temp_store.create_session(
            title="My Test",
            model="claude-sonnet-4-6",
            cwd="/home/user/project",
            api_base="https://api.example.com",
        )

        session = result["session"]
        assert session["title"] == "My Test"
        assert session["model"] == "claude-sonnet-4-6"
        assert session["cwd"] == "/home/user/project"
        assert session["api_base"] == "https://api.example.com"
        assert "id" in session
        assert "created_at" in session
        assert "updated_at" in session
        assert result["messages"] == []

        sessions = await temp_store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == session["id"]

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, temp_store):
        """Listing sessions with no data should return empty list."""
        sessions = await temp_store.list_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_list_sessions_with_data(self, temp_store):
        """Listing should return sessions sorted by updated_at desc."""
        first = await temp_store.create_session(title="First", model="gpt-5.4", cwd="/tmp", api_base=None)
        second = await temp_store.create_session(title="Second", model="gpt-5.4", cwd="/tmp", api_base=None)
        await temp_store.append_message(
            first["session"]["id"],
            {"role": "user", "content": [{"type": "text", "text": "first"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )
        await temp_store.append_message(
            second["session"]["id"],
            {"role": "user", "content": [{"type": "text", "text": "second"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        sessions = await temp_store.list_sessions()
        assert len(sessions) == 2
        # Should be sorted by updated_at descending (newest first)
        assert sessions[0]["updated_at"] >= sessions[1]["updated_at"]

    @pytest.mark.asyncio
    async def test_latest_session_returns_newest_match(self, temp_store):
        """latest_session should return the most recently updated session."""
        first = await temp_store.create_session(title="First", model="gpt-5.4", cwd="/tmp", api_base=None)
        second = await temp_store.create_session(title="Second", model="gpt-5.4", cwd="/tmp", api_base=None)
        await temp_store.append_message(
            first["session"]["id"],
            {"role": "user", "content": [{"type": "text", "text": "bump first"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        latest = await temp_store.latest_session(cwd="/tmp")
        assert latest is not None
        assert latest["id"] == first["session"]["id"]
        assert latest["id"] != second["session"]["id"]

    @pytest.mark.asyncio
    async def test_load_session_not_found(self, temp_store):
        """Loading non-existent session should return None."""
        result = await temp_store.load_session("non-existent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_session_with_messages(self, temp_store):
        """Loading session should restore persisted messages."""
        # Create session
        result = await temp_store.create_session(title="Test", model="gpt-5.4", cwd="/tmp", api_base=None)
        session_id = result["session"]["id"]

        # Append some messages
        await temp_store.append_message(
            session_id,
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )
        await temp_store.append_message(
            session_id,
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        # Load and verify
        loaded = await temp_store.load_session(session_id)
        assert loaded is not None
        assert len(loaded["messages"]) == 2
        assert loaded["messages"][0]["role"] == "user"
        assert loaded["messages"][0]["content"] == [{"type": "text", "text": "Hello"}]
        assert loaded["messages"][1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_load_session_repairs_interrupted_tool_loop(self, temp_store):
        """Loading should append a synthetic result for an interrupted tool loop."""

        result = await temp_store.create_session(title="Test", model="gpt-5.4", cwd="/tmp", api_base=None)
        session_id = result["session"]["id"]

        await temp_store.append_message(
            session_id,
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
            },
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        loaded = await temp_store.load_session(session_id)

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
                        "model_text": "error: tool call was interrupted (no result recorded)",
                        "display_text": "Tool call was interrupted before it returned a result",
                        "is_error": True,
                    }
                ],
            },
        ]

        loaded_again = await temp_store.load_session(session_id)
        assert loaded_again is not None
        assert loaded_again["messages"] == loaded["messages"]

    @pytest.mark.asyncio
    async def test_append_message_updates_title(self, temp_store):
        """First user message should auto-update session title."""
        result = await temp_store.create_session(title="New chat", model="gpt-5.4", cwd="/tmp", api_base=None)
        session_id = result["session"]["id"]

        await temp_store.append_message(
            session_id,
            {"role": "user", "content": [{"type": "text", "text": "How do I write a Python function?"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        loaded = await temp_store.load_session(session_id)
        assert loaded["session"]["title"] == "How do I write a Python function?"

    @pytest.mark.asyncio
    async def test_clear_session(self, temp_store):
        """Clearing session should remove all messages but keep meta."""
        result = await temp_store.create_session(title="Test", model="gpt-5.4", cwd="/tmp", api_base=None)
        session_id = result["session"]["id"]

        await temp_store.append_message(
            session_id,
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )
        await temp_store.clear_session(session_id)

        loaded = await temp_store.load_session(session_id)
        assert loaded["messages"] == []
        assert loaded["session"]["title"] == "Test"  # Meta preserved

    @pytest.mark.asyncio
    async def test_delete_session(self, temp_store):
        """Deleting session should remove all files."""
        result = await temp_store.create_session(title="Test", model="gpt-5.4", cwd="/tmp", api_base=None)
        session_id = result["session"]["id"]

        session_dir = temp_store.session_dir(session_id)
        await temp_store.append_message(
            session_id,
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )
        assert session_dir.exists()

        await temp_store.delete_session(session_id)

        assert not session_dir.exists()
        assert await temp_store.load_session(session_id) is None

    @pytest.mark.asyncio
    async def test_draft_session_is_not_saved_until_first_message(self, temp_store):
        """Draft sessions should stay in memory until a message is persisted."""
        result = temp_store.draft_session(title="Test", model="gpt-5.4", cwd="/tmp", api_base=None)

        assert await temp_store.load_session(result["session"]["id"]) is None
        assert await temp_store.list_sessions() == []

    @pytest.mark.asyncio
    async def test_message_storage_format(self, temp_store):
        """Messages should be stored as valid JSONL."""
        result = await temp_store.create_session(title="Test", model="gpt-5.4", cwd="/tmp", api_base=None)
        session_id = result["session"]["id"]

        msg = {"role": "user", "content": [{"type": "text", "text": "Test message"}]}
        await temp_store.append_message(
            session_id,
            msg,
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        # Read raw JSONL file
        messages_path = temp_store.messages_path(session_id)
        lines = messages_path.read_text().strip().split("\n")
        assert len(lines) == 1

        parsed = json.loads(lines[0])
        assert parsed["role"] == "user"
        assert parsed["content"] == [{"type": "text", "text": "Test message"}]


class TestSessionStoreEdgeCases:
    """Edge case tests for SessionStore."""

    @pytest.mark.asyncio
    async def test_list_sessions_filtered_by_cwd(self, temp_store):
        """Listing with cwd filter should only return matching sessions."""
        project = await temp_store.create_session(
            title="In Project",
            model="gpt-5.4",
            cwd="/home/user/project",
            api_base=None,
        )
        home = await temp_store.create_session(
            title="In Home",
            model="gpt-5.4",
            cwd="/home/user",
            api_base=None,
        )
        await temp_store.append_message(
            project["session"]["id"],
            {"role": "user", "content": [{"type": "text", "text": "project"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/home/user/project",
            api_base=None,
        )
        await temp_store.append_message(
            home["session"]["id"],
            {"role": "user", "content": [{"type": "text", "text": "home"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/home/user",
            api_base=None,
        )

        sessions = await temp_store.list_sessions(cwd="/home/user/project")
        assert len(sessions) == 1
        assert sessions[0]["title"] == "In Project"

    @pytest.mark.asyncio
    async def test_append_message_creates_directories(self, temp_store):
        """Appending to non-existent session should create directories."""
        store = SessionStore(data_dir=temp_store.data_dir)
        session_id = "brand-new-session"

        # Session doesn't exist yet
        await store.append_message(
            session_id,
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            provider="anthropic",
            model="gpt-5.4",
            cwd="/tmp",
            api_base=None,
        )

        assert (store.session_dir(session_id) / "tool-output").exists()
        assert store.messages_path(session_id).exists()
