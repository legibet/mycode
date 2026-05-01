"""Focused tests for conversation context compaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycode.agent import Agent
from mycode.session import (
    DEFAULT_COMPACT_THRESHOLD,
    SessionStore,
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


def make_agent(tmp_path: Path) -> Agent:
    return Agent(model="m", provider="anthropic", cwd="/tmp", session_dir=tmp_path)


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
async def test_load_session_keeps_compact_marker_inline(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")

    raw_messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("old summary", provider="p", model="m"),
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
        build_compact_event("new summary", provider="p", model="m"),
        {"role": "assistant", "content": [{"type": "text", "text": "latest reply"}]},
    ]
    for message in raw_messages:
        await store.append_message("s1", message)

    loaded = await store.load_session("s1")

    assert loaded is not None
    messages = loaded["messages"]

    # Visible state is the raw JSONL (no rewind events here): pre-compact
    # history stays intact, compact events remain inline as markers.
    assert [m.get("role") for m in messages] == [
        "user",
        "assistant",
        "compact",
        "user",
        "compact",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_compact_event_remains_in_raw_jsonl(store: SessionStore) -> None:
    await store.create_session("s1", cwd="/tmp")

    for message in [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("summary", provider="p", model="m"),
    ]:
        await store.append_message("s1", message)

    raw_lines = store.messages_path("s1").read_text().strip().splitlines()

    assert len(raw_lines) == 3
    assert json.loads(raw_lines[2])["role"] == "compact"


def test_project_for_provider_is_identity_without_compact(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]

    assert agent._project_for_provider(messages) is messages


def test_project_for_provider_continues_when_tail_is_empty(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "early"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        build_compact_event("summary", provider="p", model="m"),
    ]

    projected = agent._project_for_provider(messages)

    assert [m["role"] for m in projected] == ["user"]
    text = projected[0]["content"][0]["text"]
    assert "summary" in text
    assert "Resume directly" in text


def test_project_for_provider_continues_when_tail_starts_with_assistant(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "early"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        build_compact_event("summary", provider="p", model="m"),
        {"role": "assistant", "content": [{"type": "text", "text": "tail"}]},
    ]

    projected = agent._project_for_provider(messages)

    assert [m["role"] for m in projected] == ["user", "assistant"]
    assert "Resume directly" in projected[0]["content"][0]["text"]
    assert projected[1]["content"][0]["text"] == "tail"


def test_project_for_provider_inserts_ack_when_tail_starts_with_user(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "early"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        build_compact_event("summary", provider="p", model="m"),
        {"role": "user", "content": [{"type": "text", "text": "follow-up"}]},
    ]

    projected = agent._project_for_provider(messages)

    assert [m["role"] for m in projected] == ["user", "assistant", "user"]
    assert "Resume directly" not in projected[0]["content"][0]["text"]
    assert projected[1]["content"][0]["text"] == "Acknowledged."
    assert projected[2]["content"][0]["text"] == "follow-up"


def test_project_for_provider_uses_latest_compact_and_drops_earlier_markers(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "very old"}]},
        build_compact_event("EARLIER_SUMMARY", provider="p", model="m"),
        {"role": "assistant", "content": [{"type": "text", "text": "mid"}]},
        build_compact_event("LATEST_SUMMARY", provider="p", model="m"),
        {"role": "assistant", "content": [{"type": "text", "text": "tail"}]},
    ]

    projected = agent._project_for_provider(messages)

    assert [m["role"] for m in projected] == ["user", "assistant"]
    summary_user_text = projected[0]["content"][0]["text"]
    assert "LATEST_SUMMARY" in summary_user_text
    assert "EARLIER_SUMMARY" not in summary_user_text


def test_agent_uses_default_compact_threshold(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    assert agent.compact_threshold == DEFAULT_COMPACT_THRESHOLD
