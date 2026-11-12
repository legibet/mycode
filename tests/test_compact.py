"""Focused tests for conversation context compaction."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Unpack
from unittest.mock import patch

import pytest

from mycode import CompactEvent, UsageEvent, tool
from mycode.agent import Agent
from mycode.compact import NothingToCompactError, build_marker, find_keep_from, project
from mycode.messages import ConversationMessage, MessageMeta, flatten_message_text, text_block
from mycode.providers.base import MessageDone, ProviderRequest, ProviderStreamEvent
from mycode.session import SessionStore


@tool
def ping() -> str:
    """Answer pong."""

    return "pong"


@tool
def dump(size: int) -> str:
    """Return ``size`` characters."""

    return "x" * size


class _Adapter:
    """Answers each request with the next scripted response and records the requests."""

    def __init__(self, *responses: ConversationMessage | Exception):
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(replace(request, messages=list(request.messages)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        yield MessageDone(copy.deepcopy(response))


def _reply(text: str, **meta: Unpack[MessageMeta]) -> ConversationMessage:
    return {"role": "assistant", "content": [text_block(text)], "meta": meta}


def _call(call_id: str, size: int, **meta: Unpack[MessageMeta]) -> ConversationMessage:
    """A response calling ``dump`` for ``size`` characters of tool output."""

    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": call_id, "name": "dump", "input": {"size": size}}],
        "meta": {"stop_reason": "tool_use", **meta},
    }


def _user(text: str) -> ConversationMessage:
    return {"role": "user", "content": [text_block(text)]}


def _marker(summary: str = "summary", *, keep_from: int, **overrides: Any) -> ConversationMessage:
    return build_marker(
        summary, trigger="auto", provider="p", model="m", context_window=100, keep_from=keep_from, **overrides
    )


def _roles(messages: list[ConversationMessage]) -> list[str]:
    return [message["role"] for message in messages]


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path / "sessions")


def make_agent(tmp_path: Path) -> Agent:
    return Agent(model="m", provider="anthropic", session_dir=tmp_path)


@pytest.mark.asyncio
async def test_session_load_keeps_compact_markers_inline_and_append_only(store: SessionStore) -> None:
    raw_messages: list[ConversationMessage] = [
        _user("hello"),
        _reply("hi"),
        _marker("old summary", keep_from=1),
        _user("next"),
        _marker("new summary", keep_from=3),
        _reply("latest reply"),
    ]
    for message in raw_messages:
        await store.append_message("s1", message)

    loaded = await store.load_messages("s1")
    raw_lines = store.messages_path("s1").read_text(encoding="utf-8").strip().splitlines()

    expected = ["user", "assistant", "compact", "user", "compact", "assistant"]
    assert _roles(loaded) == expected
    assert [json.loads(line)["role"] for line in raw_lines] == expected


# ---------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "expected_roles", "expected_texts", "continues_directly"),
    [
        pytest.param([], ["user"], [], True, id="nothing-after"),
        pytest.param([_reply("next")], ["user", "assistant"], ["next"], True, id="assistant-after"),
        pytest.param(
            [_user("follow-up")],
            ["user", "assistant", "user"],
            ["Acknowledged.", "follow-up"],
            False,
            id="user-after",
        ),
    ],
)
def test_projection_keeps_role_order_after_summary(
    after: list[ConversationMessage],
    expected_roles: list[str],
    expected_texts: list[str],
    continues_directly: bool,
) -> None:
    projected = project([_user("early"), _reply("ok"), _marker(keep_from=2), *after], None)

    assert _roles(projected) == expected_roles
    summary = projected[0]["content"][0]
    assert summary["type"] == "text"
    assert "summary" in summary["text"]
    assert ("Resume directly" in summary["text"]) is continues_directly
    assert [message["content"] for message in projected[1:]] == [[text_block(text)] for text in expected_texts]


def test_projection_uses_latest_summary_and_transcript_hint() -> None:
    messages = [
        _user("very old"),
        _marker("EARLIER_SUMMARY", keep_from=1),
        _reply("mid"),
        _marker("LATEST_SUMMARY", keep_from=3),
        _reply("next"),
    ]

    projected = project(messages, "/sessions/s1/messages.jsonl")

    assert _roles(projected) == ["user", "assistant"]
    summary = projected[0]["content"][0]
    assert summary["type"] == "text"
    assert "LATEST_SUMMARY" in summary["text"]
    assert "EARLIER_SUMMARY" not in summary["text"]
    assert "/sessions/s1/messages.jsonl" in summary["text"]


def test_projection_keeps_the_tail_of_the_latest_marker_only() -> None:
    first = [_user("first"), _reply("one"), _user("second"), _reply("two"), _marker("S1", keep_from=2)]
    second = [*first, _user("third"), _reply("three"), _marker("S2", keep_from=5), _user("fourth")]

    def texts(projected: list[ConversationMessage]) -> list[str]:
        return [block["text"] for message in projected[1:] for block in message["content"] if block["type"] == "text"]

    projected = project(first, None)
    assert "S1" in flatten_message_text(projected[0])
    assert texts(projected) == ["Acknowledged.", "second", "two"]

    projected = project(second, None)
    assert "S2" in flatten_message_text(projected[0])
    assert texts(projected) == ["Acknowledged.", "third", "three", "fourth"]

    # A tail that starts at an assistant message follows the summary directly.
    projected = project([*first[:4], _marker("S1", keep_from=3)], None)
    assert "Resume directly" in flatten_message_text(projected[0])
    assert texts(projected) == ["two"]


def _tool_round(call_id: str, output_chars: int, text: str = "") -> list[ConversationMessage]:
    call: ConversationMessage = {
        "role": "assistant",
        "content": [text_block(text), {"type": "tool_use", "id": call_id, "name": "dump", "input": {}}],
    }
    result: ConversationMessage = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": call_id, "output": "x" * output_chars, "is_error": False}],
    }
    return [call, result]


@pytest.mark.parametrize(
    ("context_window", "expected"),
    [
        # Budget 20_000: everything after the latest marker fits, so only the newest step stays.
        pytest.param(200_000, 6, id="everything-fits"),
        # Budget 11_500: both tool rounds fit, the prompt before them does not.
        pytest.param(115_000, 4, id="earlier-step-fits"),
        # Budget 10_000: the older tool result still fits but cannot start the
        # tail, and its call does not fit, so the tail starts at the newer call.
        pytest.param(100_000, 6, id="never-starts-at-tool-result"),
        # Budget 3_000: the latest tool result alone is too large, and stays with its call.
        pytest.param(30_000, 6, id="newest-step-over-budget"),
    ],
)
def test_tail_respects_the_budget_and_keeps_tool_results_with_their_calls(context_window: int, expected: int) -> None:
    messages = [
        _user("old " * 1_000),
        _reply("old answer"),
        _marker(keep_from=1),
        _user("p" * 4_000),  # ~1_000 tokens
        *_tool_round("c1", 20_000, text="a" * 8_000),  # ~2_000 + 5_000 tokens
        *_tool_round("c2", 16_000),  # ~0 + 4_000 tokens
    ]

    assert find_keep_from(messages, context_window) == expected


def test_tail_keeps_an_unanswered_prompt_larger_than_the_budget() -> None:
    messages = [_user("old"), _reply("old answer"), _user("p" * 100_000)]

    assert find_keep_from(messages, 100_000) == 2


# ---------------------------------------------------------------------
# Summary request
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acompact_sends_the_request_cut_at_the_tail_plus_one_message_and_persists_marker(tmp_path: Path) -> None:
    agent = Agent(
        model="m",
        provider="anthropic",
        session_dir=tmp_path,
        compact_threshold=0,
        system="SYSTEM",
        reasoning_effort="high",
        max_tokens=32_000,
        tools=[ping],
    )
    persisted: list[ConversationMessage] = []
    store_lines_at_callback: list[int] = []
    messages_path = SessionStore(data_dir=tmp_path).messages_path(agent.session_id)

    async def on_persist(message: ConversationMessage) -> None:
        persisted.append(message)
        text = messages_path.read_text(encoding="utf-8") if messages_path.exists() else ""
        store_lines_at_callback.append(len(text.strip().splitlines()) if text.strip() else 0)

    adapter = _Adapter(_reply("chat reply"), _reply("THE_SUMMARY"))
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for _ in agent.achat("hello there"):
            pass
        marker = await agent.acompact(on_persist=on_persist)

    assert marker["content"] == [text_block("THE_SUMMARY")]

    # Same system, tools, effort and max_tokens as a normal request, so the
    # provider can reuse the cached prefix. The messages stop where the tail
    # starts, and one user message is added.
    chat_request, summary_request = adapter.requests
    assert replace(summary_request, messages=[]) == replace(chat_request, messages=[])
    assert marker["meta"]["keep_from"] == 1
    assert summary_request.messages[:-1] == agent.messages[:1]
    assert summary_request.messages[-1]["role"] == "user"
    assert [block["type"] for block in summary_request.messages[-1]["content"]] == ["text"]

    # on_persist ran before the SDK store write.
    assert persisted == [marker]
    assert store_lines_at_callback == [2]

    # Marker is appended to memory and disk exactly once.
    assert agent.messages[-1] is marker
    raw_roles = [json.loads(line)["role"] for line in messages_path.read_text(encoding="utf-8").strip().splitlines()]
    assert raw_roles == ["user", "assistant", "compact"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    [
        pytest.param([_user("hi"), _reply("hello"), _marker(keep_from=1)], id="nothing-after-the-marker"),
        pytest.param([_user("hi")], id="only-the-newest-step"),
    ],
)
async def test_acompact_with_nothing_to_summarize_raises_before_provider_request(
    tmp_path: Path, history: list[ConversationMessage]
) -> None:
    agent = make_agent(tmp_path)
    agent.messages.extend(history)

    adapter = _Adapter()
    with patch("mycode.agent.get_provider_adapter", return_value=adapter), pytest.raises(NothingToCompactError):
        await agent.acompact()

    assert adapter.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_reply("half a summ", stop_reason="length"), id="truncated"),
        pytest.param(_reply("half a summ", stop_reason="error"), id="error-response"),
        pytest.param(_call("c1", 10), id="tool-call-only"),
    ],
)
async def test_acompact_rejects_unusable_summary_and_persists_nothing(
    tmp_path: Path, response: ConversationMessage
) -> None:
    agent = Agent(model="m", provider="anthropic", session_dir=tmp_path, tools=[dump])
    agent.messages.extend([_user("hi"), _reply("hello")])

    with (
        patch("mycode.agent.get_provider_adapter", return_value=_Adapter(response)),
        pytest.raises(ValueError, match="summary"),
    ):
        await agent.acompact()

    # No marker, and the summary response's tool call was not executed.
    assert _roles(agent.messages) == ["user", "assistant"]
    assert not SessionStore(data_dir=tmp_path).session_exists(agent.session_id)


@pytest.mark.asyncio
async def test_acompact_cancellation_writes_no_marker(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent.messages.extend([_user("hi"), _reply("hello")])
    messages_path = SessionStore(data_dir=tmp_path).messages_path(agent.session_id)

    class _HangThenSummary:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.hang = True

        async def stream_turn(self, _request: Any) -> AsyncIterator[ProviderStreamEvent]:
            self.entered.set()
            if self.hang:
                # A late summary must not be committed after cancellation.
                with suppress(asyncio.CancelledError):
                    await asyncio.Event().wait()
            yield MessageDone(_reply("SUMMARY"))

    adapter = _HangThenSummary()
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        compact_task = asyncio.create_task(agent.acompact())
        async with asyncio.timeout(2):
            await adapter.entered.wait()
            with pytest.raises(RuntimeError):
                await anext(agent.achat("intrude"))
            with pytest.raises(RuntimeError):
                agent.clear()
            agent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await compact_task
        assert all(m.get("role") != "compact" for m in agent.messages)
        assert not messages_path.exists()

        adapter.hang = False
        async with asyncio.timeout(2):
            marker = await agent.acompact()

    assert marker["content"] == [text_block("SUMMARY")]


# ---------------------------------------------------------------------
# Automatic compaction
# ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_size", "compact_threshold", "expected_compaction"),
    [
        # 79_000 reported + 4_000 chars / 4 of tool output reaches 80% of 100_000.
        pytest.param(4_000, None, True, id="tool-output-crosses-threshold"),
        pytest.param(3_000, None, False, id="below-threshold"),
        pytest.param(4_000, 0, False, id="disabled"),
    ],
)
async def test_trigger_counts_messages_added_after_the_last_usage(
    output_size: int,
    compact_threshold: float | None,
    expected_compaction: bool,
) -> None:
    agent = Agent(
        model="m",
        provider="anthropic",
        context_window=100_000,
        compact_threshold=compact_threshold,
        tools=[dump],
    )
    summary = [_reply("SUMMARY")] if expected_compaction else []
    adapter = _Adapter(_call("c1", output_size, usage={"total_tokens": 79_000}), *summary, _reply("done"))

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("hello")]

    assert ("compact" in [event.type for event in events]) is expected_compaction
    # The marker lands before the request that would have crossed the threshold.
    compact = ["compact"] if expected_compaction else []
    assert _roles(agent.messages) == ["user", "assistant", "user", *compact, "assistant"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "expected_compaction"),
    [
        pytest.param([_user("old"), _reply("old answer", usage={"total_tokens": 95_000})], True, id="resumed"),
        pytest.param(
            [
                _user("old"),
                _reply("old answer", usage={"total_tokens": 95_000}),
                _marker(keep_from=1, usage={"total_tokens": 95_000}),
            ],
            False,
            id="no-usage-after-marker",
        ),
    ],
)
async def test_check_runs_before_the_first_request_only_with_usage_after_the_latest_marker(
    history: list[ConversationMessage], expected_compaction: bool
) -> None:
    agent = Agent(model="m", provider="anthropic", context_window=100_000, messages=history)
    summary = [_reply("SUMMARY")] if expected_compaction else []
    adapter = _Adapter(*summary, _reply("done"))

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("hello")]

    assert ("compact" in [event.type for event in events]) is expected_compaction
    assert _roles(agent.messages[len(history) :]) == [
        "user",
        *(["compact"] if expected_compaction else []),
        "assistant",
    ]


@pytest.mark.asyncio
async def test_auto_compact_usage_counts_into_the_turn(tmp_path: Path) -> None:
    agent = Agent(model="m", provider="anthropic", context_window=100_000, session_dir=tmp_path, tools=[ping])
    adapter = _Adapter(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "ping", "input": {}}],
            "meta": {
                "stop_reason": "tool_use",
                "created_at": "2020-01-01T00:00:01+00:00",
                "usage": {"total_tokens": 80_000, "input_tokens": 79_000, "output_tokens": 1_000},
                "cost": {"total": 0.01},
            },
        },
        _reply(
            "summary",
            usage={"total_tokens": 80_500, "input_tokens": 80_000, "output_tokens": 500},
            cost={"total": 0.005},
        ),
        _reply("done"),
    )
    # Separate the response time from the compact commit without sleeping.
    started_at = "2020-01-01T00:00:00+00:00"

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat({**_user("hello"), "meta": {"created_at": started_at}})]

    assert [event.type for event in events] == ["usage", "tool_start", "tool_done", "compact", "usage", "usage"]
    assert isinstance(events[0], UsageEvent)
    assert events[0].turn_duration_ms == 1000
    assert events[3] == CompactEvent(trigger="auto")
    compact_usage = events[4]
    assert isinstance(compact_usage, UsageEvent)
    # The summary call is billed into the turn, but the context metric keeps
    # the last normal request's total — the summary total is not context size.
    assert compact_usage.context_tokens == 80_000
    assert compact_usage.turn_usage == {"total_tokens": 160_500, "input_tokens": 159_000, "output_tokens": 1_500}
    assert compact_usage.turn_cost == pytest.approx({"total": 0.015})

    records = await SessionStore(data_dir=tmp_path).load_raw_messages(agent.session_id)
    assert records == agent.messages
    marker = records[3]
    assert marker["role"] == "compact"
    assert marker["meta"]["trigger"] == "auto"
    elapsed = datetime.fromisoformat(marker["meta"]["created_at"]) - datetime.fromisoformat(started_at)
    assert compact_usage.turn_duration_ms == int(elapsed.total_seconds() * 1000)
    assert marker["meta"]["usage"] == {"total_tokens": 80_500, "input_tokens": 80_000, "output_tokens": 500}
    assert marker["meta"]["cost"] == {"total": 0.005}


@pytest.mark.asyncio
async def test_compaction_summarizes_the_messages_before_the_tail_and_keeps_the_tail() -> None:
    agent = Agent(model="m", provider="anthropic", context_window=100_000, tools=[dump])
    adapter = _Adapter(
        _call("c1", 60_000, usage={"total_tokens": 10_000}),
        _call("c2", 4_000, usage={"total_tokens": 79_000}),
        _reply("SUMMARY"),
        _reply("done"),
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for _ in agent.achat("hello"):
            pass

    # The first step is over the budget, so the tail is the second step.
    assert agent.messages[5]["meta"]["keep_from"] == 3
    assert adapter.requests[2].messages[:-1] == agent.messages[:3]
    after = adapter.requests[3].messages
    assert _roles(after) == ["user", "assistant", "user"]
    assert after[1:] == agent.messages[3:5]

    adapter.responses = [_reply("MANUAL")]
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        marker = await agent.acompact()

    # Only the answer follows the automatic marker. The manual summary takes in
    # the earlier tail, and the answer stays after it.
    assert marker["meta"]["keep_from"] == 6
    assert adapter.requests[4].messages[1:-1] == agent.messages[3:5]
    assert project(agent.messages, None)[1:] == [agent.messages[6]]


@pytest.mark.asyncio
async def test_failed_auto_compact_is_not_retried_in_the_same_turn() -> None:
    usage: MessageMeta = {"usage": {"total_tokens": 90_000, "input_tokens": 89_000, "output_tokens": 1_000}}
    agent = Agent(model="m", provider="anthropic", context_window=100_000, tools=[dump])
    adapter = _Adapter(
        _call("c1", 10, **usage),
        ValueError("summary failed"),
        _call("c2", 10, **usage),
        _reply("done"),
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("hello")]

    # One summary attempt, then the turn goes on with the full history.
    assert len(adapter.requests) == 4
    assert "compact" not in [event.type for event in events]
    assert _roles(agent.messages) == ["user", "assistant", "user", "assistant", "user", "assistant"]
    final_usage = [event for event in events if isinstance(event, UsageEvent)][-1]
    assert final_usage.turn_usage == {"total_tokens": 180_000, "input_tokens": 178_000, "output_tokens": 2_000}

    # The next turn tries again.
    adapter.responses = [_reply("SUMMARY"), _reply("done again")]
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("next")]

    assert "compact" in [event.type for event in events]


@pytest.mark.asyncio
async def test_failed_auto_compact_commit_propagates_without_publishing_marker(tmp_path: Path) -> None:
    agent = Agent(model="m", provider="anthropic", context_window=100_000, session_dir=tmp_path, tools=[dump])
    failure = OSError("compact persistence failed")

    async def persist(message: ConversationMessage) -> None:
        if message["role"] == "compact":
            raise failure

    adapter = _Adapter(_call("c1", 10, usage={"total_tokens": 80_000}), _reply("SUMMARY"))
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        stream = agent.achat("hello", on_persist=persist)
        assert [(await anext(stream)).type for _ in range(3)] == ["usage", "tool_start", "tool_done"]
        with pytest.raises(OSError, match="compact persistence failed") as exc:
            await anext(stream)

    assert exc.value is failure
    assert _roles(agent.messages) == ["user", "assistant", "user"]
    assert await SessionStore(data_dir=tmp_path).load_raw_messages(agent.session_id) == agent.messages


@pytest.mark.asyncio
async def test_legacy_total_tokens_meta_is_ignored() -> None:
    # Only meta.usage feeds compaction and the context metric.
    agent = Agent(model="m", provider="anthropic", context_window=100_000)

    adapter = _Adapter({"role": "assistant", "content": [text_block("reply")], "meta": {"total_tokens": 90_000}})
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [event async for event in agent.achat("hello")]

    assert [event.type for event in events] == ["usage"]
    assert isinstance(events[-1], UsageEvent)
    assert events[-1].context_tokens is None
