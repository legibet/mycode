"""Focused tests for conversation context compaction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mycode.agent import Agent
from mycode.compact import (
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_COMPACT_THRESHOLD,
    NothingToCompactError,
    apply_compact_replay,
    build_compact_event,
    has_compactable_history,
    should_compact,
)
from mycode.providers.base import ProviderStreamEvent
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


def test_has_compactable_history_requires_content_after_latest_marker() -> None:
    user = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    marker = build_compact_event("summary", provider="p", model="m")

    assert has_compactable_history([]) is False
    assert has_compactable_history([user]) is True
    assert has_compactable_history([user, marker]) is False
    assert (
        has_compactable_history([user, marker, {"role": "assistant", "content": [{"type": "text", "text": "tail"}]}])
        is True
    )
    assert has_compactable_history([user, marker, {"role": "user", "content": []}]) is False


class _RecordingAdapter:
    """Streams one canned summary per turn and records every request."""

    def __init__(self, summaries: list[str]):
        self.requests: list[Any] = []
        self._summaries = list(summaries)

    async def stream_turn(self, request: Any) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        text = self._summaries.pop(0)
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
        )


@pytest.mark.asyncio
async def test_acompact_persists_marker_and_next_turn_replays_summary(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    persisted: list[dict[str, Any]] = []
    store_lines_at_callback: list[int] = []
    messages_path = SessionStore(data_dir=tmp_path).messages_path(agent.session_id)

    async def on_persist(message: dict[str, Any]) -> None:
        persisted.append(message)
        text = messages_path.read_text(encoding="utf-8") if messages_path.exists() else ""
        store_lines_at_callback.append(len(text.strip().splitlines()) if text.strip() else 0)

    # First canned text answers the chat turn; the second one is the summary.
    adapter = _RecordingAdapter(["chat reply", "THE_SUMMARY"])
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for _ in agent.achat("hello there"):
            pass
        marker = await agent.acompact(on_persist=on_persist)

    assert marker["role"] == "compact"
    assert marker["content"][0]["text"] == "THE_SUMMARY"

    # The summary request carries no tools and no reasoning effort.
    compact_request = adapter.requests[1]
    assert compact_request.tools == []
    assert compact_request.reasoning_effort is None
    assert compact_request.append_messages[-1]["content"][0]["text"] == COMPACT_SUMMARY_PROMPT

    # on_persist ran before the SDK store write.
    assert persisted == [marker]
    assert store_lines_at_callback == [2]

    # Marker is appended to memory and disk exactly once.
    assert agent.messages[-1] is marker
    raw_roles = [json.loads(line)["role"] for line in messages_path.read_text(encoding="utf-8").strip().splitlines()]
    assert raw_roles == ["user", "assistant", "compact"]

    # The next provider request replays the summary instead of the full history.
    projected = apply_compact_replay(agent.messages)
    assert [m["role"] for m in projected] == ["user"]
    assert "THE_SUMMARY" in projected[0]["content"][0]["text"]
    assert "hello there" not in projected[0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_acompact_ignores_compact_threshold(tmp_path: Path) -> None:
    agent = Agent(model="m", provider="anthropic", cwd="/tmp", session_dir=tmp_path, compact_threshold=0)
    agent.messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})

    adapter = _RecordingAdapter(["SUMMARY"])
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        marker = await agent.acompact()

    assert marker["content"][0]["text"] == "SUMMARY"


@pytest.mark.asyncio
async def test_acompact_without_new_context_raises_before_provider_request(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent.messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    agent.messages.append(build_compact_event("summary", provider="p", model="m"))

    adapter = _RecordingAdapter([])
    with patch("mycode.agent.get_provider_adapter", return_value=adapter), pytest.raises(NothingToCompactError):
        await agent.acompact()

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_acompact_cancellation_writes_no_marker(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent.messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    messages_path = SessionStore(data_dir=tmp_path).messages_path(agent.session_id)

    class _CancellingAdapter:
        async def stream_turn(self, _request: Any) -> AsyncIterator[ProviderStreamEvent]:
            agent.cancel()
            yield ProviderStreamEvent(
                "message_done",
                {"message": {"role": "assistant", "content": [{"type": "text", "text": "SUMMARY"}]}},
            )

    with (
        patch("mycode.agent.get_provider_adapter", return_value=_CancellingAdapter()),
        pytest.raises(asyncio.CancelledError),
    ):
        await agent.acompact()

    assert all(m.get("role") != "compact" for m in agent.messages)
    assert not messages_path.exists()


def test_sync_compact_returns_marker(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent.messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})

    adapter = _RecordingAdapter(["SUMMARY"])
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        marker = agent.compact()

    assert marker["role"] == "compact"
    assert marker["content"] == [{"type": "text", "text": "SUMMARY"}]
    assert marker["meta"]["provider"] == "anthropic"
    assert marker["meta"]["model"] == "m"
