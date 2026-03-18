"""Tests for CLI output behavior."""

import pytest

from mycode.cli import _history_preview_entries, resolve_cli_session, run_once
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


async def test_run_once_ignores_reasoning_output(monkeypatch):
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
    assert "Hidden reasoning" not in printed
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
    await store.append_message(second["session"]["id"], {"role": "user", "content": "hello"})

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
    assert resolved.messages[0]["content"] == "hello"


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
            {"role": "user", "content": "Inspect project"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "function": {"name": "read", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "bash", "arguments": "{}"}},
                ],
            },
        ]
    )

    assert entries == [
        ("You", "Inspect project"),
        ("Assistant", "[Used tools: read, bash]"),
    ]
