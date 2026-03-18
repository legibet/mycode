"""Tests for CLI output behavior."""

import pytest

from mycode.cli import (
    _history_preview_entries,
    _resolve_session_choice,
    list_cli_sessions,
    resolve_cli_session,
    run_once,
)
from mycode.core.agent import Event
from mycode.core.session import SessionStore


class _FakeConsole:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def print(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class _FakeStore:
    async def append_message(self, session_id: str, payload: dict) -> None:
        return None


class _FakeAgent:
    async def achat(self, message: str, *, on_persist=None):
        yield Event("reasoning", {"content": "Hidden reasoning"})
        yield Event("text", {"content": "Visible answer"})


async def test_run_once_prints_reasoning_output(monkeypatch):
    fake_console = _FakeConsole()
    monkeypatch.setattr("mycode.cli.console", fake_console)

    code = await run_once(
        _FakeAgent(),
        store=_FakeStore(),
        session_id="test-session",
        message="hello",
    )

    assert code == 0
    printed = [str(args[0]) for args, _kwargs in fake_console.calls if args]
    assert "Hidden reasoning" in printed
    assert "Visible answer" in printed


@pytest.mark.asyncio
async def test_resolve_cli_session_defaults_to_new(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    resolved = await resolve_cli_session(
        store=store,
        cwd=str(tmp_path),
        model="gpt-5.4",
        api_base=None,
        requested_session_id=None,
        continue_last=False,
    )

    assert resolved.mode == "new"
    assert resolved.messages == []
    assert resolved.session["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_resolve_cli_session_continue_reuses_latest(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    first = await store.create_session("First", model="gpt-5.4", cwd=str(tmp_path), api_base=None)
    second = await store.create_session("Second", model="gpt-5.4", cwd=str(tmp_path), api_base=None)
    await store.append_message(
        second["session"]["id"], {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    )

    resolved = await resolve_cli_session(
        store=store,
        cwd=str(tmp_path),
        model="gpt-5.4",
        api_base=None,
        requested_session_id=None,
        continue_last=True,
    )

    assert resolved.mode == "resumed"
    assert resolved.session_id != first["session"]["id"]
    assert resolved.session_id == second["session"]["id"]
    assert resolved.messages[0]["content"] == [{"type": "text", "text": "hello"}]


@pytest.mark.asyncio
async def test_resolve_cli_session_explicit_missing_id_errors(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    with pytest.raises(ValueError, match="Unknown session"):
        await resolve_cli_session(
            store=store,
            cwd=str(tmp_path),
            model="gpt-5.4",
            api_base=None,
            requested_session_id="missing",
            continue_last=False,
        )


def test_history_preview_entries_summarize_tool_only_assistant_messages():
    entries = _history_preview_entries(
        [
            {"role": "user", "content": [{"type": "text", "text": "Inspect project"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "a", "name": "read", "input": {}},
                    {"type": "tool_use", "id": "b", "name": "bash", "input": {}},
                ],
            },
        ]
    )

    assert entries == [
        ("You", "Inspect project"),
        ("Assistant", "[Used tools: read, bash]"),
    ]


def test_resolve_session_choice_accepts_index_and_id_prefix():
    sessions = [
        {"id": "abc123456789", "title": "First"},
        {"id": "def987654321", "title": "Second"},
    ]

    assert _resolve_session_choice("2", sessions) == sessions[1]
    assert _resolve_session_choice("abc123", sessions) == sessions[0]


def test_resolve_session_choice_errors_on_ambiguous_prefix():
    sessions = [
        {"id": "abc123456789", "title": "First"},
        {"id": "abc987654321", "title": "Second"},
    ]

    with pytest.raises(ValueError, match="Ambiguous session id"):
        _resolve_session_choice("abc", sessions)


@pytest.mark.asyncio
async def test_list_cli_sessions_filters_current_workspace(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")

    await store.create_session("Current", model="gpt-5.4", cwd=current_cwd, api_base=None)
    await store.create_session("Other", model="gpt-5.4", cwd=other_cwd, api_base=None)

    current = await list_cli_sessions(store=store, cwd=current_cwd, show_all=False)
    all_sessions = await list_cli_sessions(store=store, cwd=current_cwd, show_all=True)

    assert len(current) == 1
    assert current[0]["title"] == "Current"
    assert len(all_sessions) == 2
