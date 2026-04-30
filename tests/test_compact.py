"""Focused tests for conversation context compaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycode.session import (
    DEFAULT_COMPACT_THRESHOLD,
    SessionStore,
    apply_compact,
    build_compact_event,
    should_compact,
)
from mycode_cli.config import get_settings


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


def test_workspace_config_overrides_global_compact_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home" / ".mycode"
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    monkeypatch.setenv("MYCODE_HOME", str(home))

    write_file(home / "config.json", json.dumps({"default": {"compact_threshold": 0.7}}))
    write_file(project / ".mycode" / "config.json", json.dumps({"default": {"compact_threshold": 0.9}}))

    settings = get_settings(str(project))

    assert settings.compact_threshold == 0.9


def test_should_compact_respects_threshold_boundaries() -> None:
    assert should_compact(80_000, 100_000, 0.8) is True
    assert should_compact(79_999, 100_000, 0.8) is False
    assert should_compact(99_999, 100_000, 0.0) is False
    assert should_compact(None, 100_000, 0.8) is False
    assert should_compact(50_000, None, 0.8) is False


@pytest.mark.asyncio
async def test_load_session_applies_latest_compact_summary(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")

    for message in [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("old summary", provider="p", model="m", compacted_count=2),
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
        build_compact_event("new summary", provider="p", model="m", compacted_count=4),
        {"role": "assistant", "content": [{"type": "text", "text": "latest reply"}]},
    ]:
        await store.append_message("s1", message)

    loaded = await store.load_session("s1")

    assert loaded is not None
    messages = loaded["messages"]
    assert len(messages) == 2

    user_text = messages[0]["content"][0]["text"]
    assert messages[0]["role"] == "user"
    assert messages[0]["meta"] == {"synthetic": True}
    assert "new summary" in user_text
    assert "old summary" not in user_text
    assert str(store.messages_path("s1")) in user_text
    assert "Resume directly" in user_text

    assert messages[1] == {"role": "assistant", "content": [{"type": "text", "text": "latest reply"}]}


@pytest.mark.asyncio
async def test_compact_event_remains_in_raw_jsonl(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")

    for message in [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("summary", provider="p", model="m", compacted_count=2),
    ]:
        await store.append_message("s1", message)

    raw_lines = store.messages_path("s1").read_text().strip().splitlines()

    assert len(raw_lines) == 3
    assert json.loads(raw_lines[2])["role"] == "compact"


def test_apply_compact_builds_waiting_and_continue_views() -> None:
    base_messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("summary", provider="p", model="m", compacted_count=2),
    ]

    waiting = apply_compact(base_messages)

    assert [message["role"] for message in waiting] == ["user", "assistant"]
    assert waiting[0]["meta"]["synthetic"] is True
    assert waiting[1]["meta"]["synthetic"] is True
    assert "summary" in waiting[0]["content"][0]["text"]
    assert "Resume directly" not in waiting[0]["content"][0]["text"]
    assert waiting[1]["content"][0]["text"] == "Acknowledged."

    continuing = apply_compact(base_messages, continue_now=True)

    assert len(continuing) == 1
    assert continuing[0]["role"] == "user"
    assert "Resume directly" in continuing[0]["content"][0]["text"]

    replayed = apply_compact(
        [*base_messages, {"role": "assistant", "content": [{"type": "text", "text": "continued"}]}]
    )

    assert [message["role"] for message in replayed] == ["user", "assistant"]
    assert "Resume directly" in replayed[0]["content"][0]["text"]
    assert replayed[1]["content"][0]["text"] == "continued"


def test_agent_uses_default_compact_threshold(tmp_path: Path) -> None:
    from mycode.agent import Agent

    agent = Agent(model="m", provider="anthropic", cwd="/tmp", session_dir=tmp_path)

    assert agent.compact_threshold == DEFAULT_COMPACT_THRESHOLD
