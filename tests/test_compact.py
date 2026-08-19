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
    NothingToCompactError,
    apply_compact_replay,
    build_compact_event,
)
from mycode.providers.base import ProviderStreamEvent
from mycode.session import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


def make_agent(tmp_path: Path) -> Agent:
    return Agent(model="m", provider="anthropic", session_dir=tmp_path)


@pytest.mark.asyncio
async def test_session_load_keeps_compact_markers_inline_and_append_only(store: SessionStore) -> None:
    raw_messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("old summary", provider="p", model="m", context_window=100),
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
        build_compact_event("new summary", provider="p", model="m", context_window=100),
        {"role": "assistant", "content": [{"type": "text", "text": "latest reply"}]},
    ]
    for message in raw_messages:
        await store.append_message("s1", message)

    loaded = await store.load_messages("s1")
    raw_lines = store.messages_path("s1").read_text(encoding="utf-8").strip().splitlines()

    assert [message.get("role") for message in loaded] == [
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
        build_compact_event("summary", provider="p", model="m", context_window=100),
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
        build_compact_event("EARLIER_SUMMARY", provider="p", model="m", context_window=100),
        {"role": "assistant", "content": [{"type": "text", "text": "mid"}]},
        build_compact_event("LATEST_SUMMARY", provider="p", model="m", context_window=100),
        {"role": "assistant", "content": [{"type": "text", "text": "tail"}]},
    ]

    projected = apply_compact_replay(messages, transcript_path="/sessions/s1/messages.jsonl")

    assert [m["role"] for m in projected] == ["user", "assistant"]
    summary_user_text = projected[0]["content"][0]["text"]
    assert "LATEST_SUMMARY" in summary_user_text
    assert "EARLIER_SUMMARY" not in summary_user_text
    assert "/sessions/s1/messages.jsonl" in summary_user_text


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


class _UsageAdapter:
    def __init__(self, total_tokens: int):
        self.total_tokens = total_tokens
        self.requests: list[Any] = []

    async def stream_turn(self, request: Any) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": [{"type": "text", "text": "reply" if len(self.requests) == 1 else "summary"}],
        }
        if len(self.requests) == 1:
            message["meta"] = {"usage": {"total_tokens": self.total_tokens}}
        yield ProviderStreamEvent("message_done", {"message": message})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total_tokens", "compact_threshold", "expected_compaction"),
    [
        pytest.param(80_000, None, True, id="default-threshold-reached"),
        pytest.param(79_999, None, False, id="below-default-threshold"),
        pytest.param(99_999, 0, False, id="disabled"),
    ],
)
async def test_achat_automatically_compacts_at_the_configured_threshold(
    tmp_path: Path,
    total_tokens: int,
    compact_threshold: float | None,
    expected_compaction: bool,
) -> None:
    agent = Agent(
        model="m",
        provider="anthropic",
        context_window=100_000,
        compact_threshold=compact_threshold,
    )
    adapter = _UsageAdapter(total_tokens)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("hello")]

    assert ("compact" in [event.type for event in events]) is expected_compaction
    assert len(adapter.requests) == (2 if expected_compaction else 1)
    assert (agent.messages[-1]["role"] == "compact") is expected_compaction


@pytest.mark.asyncio
async def test_acompact_persists_marker_and_uses_manual_request_contract(tmp_path: Path) -> None:
    agent = Agent(
        model="m",
        provider="anthropic",
        session_dir=tmp_path,
        compact_threshold=0,
    )
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


@pytest.mark.asyncio
async def test_acompact_without_new_context_raises_before_provider_request(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent.messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    agent.messages.append(build_compact_event("summary", provider="p", model="m", context_window=100))

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


class _UsageDetailAdapter:
    """First request nears the window; the summary request reports its own usage."""

    def __init__(self) -> None:
        self.requests = 0

    async def stream_turn(self, _request: Any) -> AsyncIterator[ProviderStreamEvent]:
        self.requests += 1
        if self.requests == 1:
            meta = {
                "usage": {"total_tokens": 80_000, "input_tokens": 79_000, "output_tokens": 1_000},
                "cost": {"total": 0.01},
            }
            text = "reply"
        else:
            meta = {
                "usage": {"total_tokens": 80_500, "input_tokens": 80_000, "output_tokens": 500},
                "cost": {"total": 0.005},
            }
            text = "summary"
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": text}], "meta": meta}},
        )


@pytest.mark.asyncio
async def test_auto_compact_usage_counts_into_the_turn() -> None:
    agent = Agent(model="m", provider="anthropic", context_window=100_000)
    adapter = _UsageDetailAdapter()

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("hello")]

    assert [event.type for event in events] == ["usage", "compact", "usage"]
    final_usage = events[-1].data
    # The summary call is billed into the turn, but the context metric keeps
    # the last normal request's total — the summary total is not context size.
    assert final_usage["context_tokens"] == 80_000
    assert final_usage["turn_usage"]["total_tokens"] == 160_500
    assert final_usage["turn_usage"]["input_tokens"] == 159_000
    assert final_usage["turn_usage"]["output_tokens"] == 1_500
    assert final_usage["turn_cost"] == pytest.approx({"total": 0.015})

    marker = agent.messages[-1]
    assert marker["role"] == "compact"
    assert marker["meta"]["usage"] == {"total_tokens": 80_500, "input_tokens": 80_000, "output_tokens": 500}
    assert marker["meta"]["cost"] == {"total": 0.005}


class _FailingSummaryAdapter:
    """First request nears the window; the summary request fails."""

    def __init__(self) -> None:
        self.requests = 0

    async def stream_turn(self, _request: Any) -> AsyncIterator[ProviderStreamEvent]:
        self.requests += 1
        if self.requests > 1:
            raise ValueError("summary failed")
        meta = {"usage": {"total_tokens": 80_000, "input_tokens": 79_000, "output_tokens": 1_000}}
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}], "meta": meta}},
        )


@pytest.mark.asyncio
async def test_failed_auto_compact_keeps_successful_turn_usage() -> None:
    agent = Agent(model="m", provider="anthropic", context_window=100_000)

    with patch("mycode.agent.get_provider_adapter", return_value=_FailingSummaryAdapter()):
        events = [event async for event in agent.achat("hello")]

    assert [event.type for event in events] == ["usage"]
    assert events[0].data["turn_usage"] == {
        "total_tokens": 80_000,
        "input_tokens": 79_000,
        "output_tokens": 1_000,
    }
    assert all(message.get("role") != "compact" for message in agent.messages)


class _LegacyMetaAdapter:
    """Simulates pre-usage assistant messages that only carry meta.total_tokens."""

    async def stream_turn(self, _request: Any) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(
            "message_done",
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "reply"}],
                    "meta": {"total_tokens": 90_000},
                }
            },
        )


@pytest.mark.asyncio
async def test_legacy_total_tokens_meta_is_ignored() -> None:
    # meta.total_tokens intentionally no longer feeds compaction or the
    # context metric; only meta.usage does.
    agent = Agent(model="m", provider="anthropic", context_window=100_000)

    with patch("mycode.agent.get_provider_adapter", return_value=_LegacyMetaAdapter()):
        events = [event async for event in agent.achat("hello")]

    assert [event.type for event in events] == ["usage"]
    assert events[0].data["context_tokens"] is None
