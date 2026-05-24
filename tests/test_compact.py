"""Focused tests for conversation context compaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycode.agent import Agent
from mycode.compact import (
    DEFAULT_COMPACT_THRESHOLD,
    apply_compact_replay,
    build_compact_event,
    should_compact,
)
from mycode.session import SessionStore
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
async def test_session_load_keeps_compact_markers_inline_and_append_only(store: SessionStore) -> None:
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
    raw_lines = store.messages_path("s1").read_text(encoding="utf-8").strip().splitlines()

    assert loaded is not None
    assert [message.get("role") for message in loaded["messages"]] == [
        "user",
        "assistant",
        "compact",
        "user",
        "compact",
        "assistant",
    ]
    assert [json.loads(line)["role"] for line in raw_lines] == [
        "user",
        "assistant",
        "compact",
        "user",
        "compact",
        "assistant",
    ]


@pytest.mark.parametrize(
    ("tail", "expected_roles", "expected_texts", "continues_directly"),
    [
        pytest.param([], ["user"], [], True, id="empty-tail"),
        pytest.param(
            [{"role": "assistant", "content": [{"type": "text", "text": "tail"}]}],
            ["user", "assistant"],
            ["tail"],
            True,
            id="assistant-tail",
        ),
        pytest.param(
            [{"role": "user", "content": [{"type": "text", "text": "follow-up"}]}],
            ["user", "assistant", "user"],
            ["Acknowledged.", "follow-up"],
            False,
            id="user-tail",
        ),
    ],
)
def test_compact_replay_preserves_role_order_after_summary(
    tail: list[dict[str, object]],
    expected_roles: list[str],
    expected_texts: list[str],
    continues_directly: bool,
) -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "early"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        build_compact_event("summary", provider="p", model="m"),
        *tail,
    ]

    projected = apply_compact_replay(messages)

    assert [message["role"] for message in projected] == expected_roles
    assert "summary" in projected[0]["content"][0]["text"]
    assert ("Resume directly" in projected[0]["content"][0]["text"]) is continues_directly
    assert [message["content"][0]["text"] for message in projected[1:]] == expected_texts


def test_compact_replay_uses_latest_summary_and_transcript_hint() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "very old"}]},
        build_compact_event("EARLIER_SUMMARY", provider="p", model="m"),
        {"role": "assistant", "content": [{"type": "text", "text": "mid"}]},
        build_compact_event("LATEST_SUMMARY", provider="p", model="m"),
        {"role": "assistant", "content": [{"type": "text", "text": "tail"}]},
    ]

    projected = apply_compact_replay(messages, transcript_path="/sessions/s1/messages.jsonl")

    assert [m["role"] for m in projected] == ["user", "assistant"]
    summary_user_text = projected[0]["content"][0]["text"]
    assert "LATEST_SUMMARY" in summary_user_text
    assert "EARLIER_SUMMARY" not in summary_user_text
    assert "/sessions/s1/messages.jsonl" in summary_user_text


def test_agent_uses_default_compact_threshold(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    assert agent.compact_threshold == DEFAULT_COMPACT_THRESHOLD
