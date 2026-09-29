"""Steer messages delivered at step boundaries of a running chat."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mycode import Agent, ConversationMessage, Event, SessionStore, image_block, text_block, tool
from mycode.models import estimate_cost
from mycode.providers.base import ProviderStreamEvent


class _Adapter:
    """Streams one scripted turn per request and records what each request saw."""

    def __init__(self, turns: list[list[ProviderStreamEvent]], on_request: Callable[[int], None] | None = None):
        self._turns = list(turns)
        self._on_request = on_request
        self.requests: list[list[ConversationMessage]] = []

    async def stream_turn(self, request: Any):
        self.requests.append(deepcopy(request.messages))
        if self._on_request is not None:
            self._on_request(len(self.requests))
        for event in self._turns.pop(0):
            yield event


def _turn(*blocks: dict[str, Any], meta: dict[str, Any] | None = None) -> list[ProviderStreamEvent]:
    message_meta = dict(meta or {})
    if any(block["type"] == "tool_use" for block in blocks):
        message_meta.setdefault("stop_reason", "tool_use")
    message = {"role": "assistant", "content": list(blocks), "meta": message_meta}
    texts = [ProviderStreamEvent("text_delta", {"text": block["text"]}) for block in blocks if block["type"] == "text"]
    return [*texts, ProviderStreamEvent("message_done", {"message": message})]


def _text_turn(text: str, **meta: Any) -> list[ProviderStreamEvent]:
    return _turn({"type": "text", "text": text}, meta=meta)


def _ping_turn(**meta: Any) -> list[ProviderStreamEvent]:
    return _turn({"type": "tool_use", "id": "call-1", "name": "ping", "input": {}}, meta=meta)


@tool
def ping() -> str:
    """Answer pong."""

    return "pong"


def _steer(text: str, input_id: str) -> ConversationMessage:
    return {"role": "user", "content": [text_block(text)], "meta": {"input_id": input_id}}


def _texts(message: ConversationMessage) -> list[str]:
    return [block["text"] for block in message["content"] if block["type"] == "text"]


def _new_agent(tmp_path: Path, **overrides: Any) -> Agent:
    overrides.setdefault("model", "gpt-5.5")
    overrides.setdefault("session_dir", tmp_path)
    overrides.setdefault("session_id", "session")
    overrides.setdefault("tools", [ping])
    return Agent(**overrides)


async def test_steer_is_delivered_after_the_tool_batch_and_persisted(tmp_path: Path) -> None:
    adapter = _Adapter([_ping_turn(), _text_turn("using sqlite")])
    agent = _new_agent(tmp_path)
    persisted: list[ConversationMessage] = []

    async def on_persist(message: ConversationMessage) -> None:
        persisted.append(message)

    events: list[Event] = []
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("build it", on_persist=on_persist):
            events.append(event)
            if event.type == "tool_start":
                assert agent.steer(_steer("use sqlite", "c1")) is True

    types = [event.type for event in events if event.type != "usage"]
    assert types == ["tool_start", "tool_done", "user_message", "text"]
    assert [message["role"] for message in agent.messages] == ["user", "assistant", "user", "user", "assistant"]
    tool_results, steer = agent.messages[2], agent.messages[3]
    assert tool_results["content"][0]["type"] == "tool_result"
    assert _texts(steer) == ["use sqlite"]
    assert steer["meta"]["steer"] is True
    assert steer["meta"]["input_ids"] == ["c1"]
    assert next(event for event in events if event.type == "user_message").data == {"message": steer}
    # The request after the batch sees the tool results, then the steer.
    assert adapter.requests[1][-2:] == [tool_results, steer]
    assert persisted == agent.messages
    assert SessionStore(data_dir=tmp_path).load_raw_messages_sync(agent.session_id) == agent.messages
    assert agent.pending_steers() == []


async def test_steer_sent_during_the_final_usage_event_is_delivered(tmp_path: Path) -> None:
    adapter = _Adapter([_text_turn("done"), _text_turn("again")])
    agent = _new_agent(tmp_path)

    steered = False
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("hello"):
            if event.type == "usage" and not steered:
                steered = agent.steer("one more thing")

    assert steered
    assert [message["role"] for message in agent.messages] == ["user", "assistant", "user", "assistant"]
    steer = agent.messages[2]
    assert _texts(steer) == ["one more thing"]
    assert steer["meta"]["steer"] is True
    assert steer["meta"]["input_ids"] == []
    assert _texts(agent.messages[3]) == ["again"]
    # Once the chat has finished, nothing accepts a steer.
    assert agent.steer("too late") is False
    assert agent.pending_steers() == []


async def test_steer_after_automatic_compaction_lands_after_the_marker(tmp_path: Path) -> None:
    adapter = _Adapter(
        [
            _text_turn("reply", usage={"total_tokens": 80_000}),
            _text_turn("summary"),
            _text_turn("steered reply"),
        ]
    )
    agent = _new_agent(tmp_path, context_window=100_000)

    events: list[Event] = []
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("hello"):
            events.append(event)
            if event.type == "text" and event.data["delta"] == "reply":
                assert agent.steer("keep going") is True

    types = [event.type for event in events]
    assert types.index("compact") < types.index("user_message")
    assert [message["role"] for message in agent.messages] == ["user", "assistant", "compact", "user", "assistant"]
    assert agent.messages[3]["meta"]["steer"] is True
    # The request after compaction replays the summary, then the steer.
    assert _texts(adapter.requests[2][-1]) == ["keep going"]


async def test_pending_steers_merge_into_one_message(tmp_path: Path) -> None:
    adapter = _Adapter([_ping_turn(), _text_turn("ok")])
    agent = _new_agent(tmp_path)

    events: list[Event] = []
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("go"):
            events.append(event)
            if event.type == "tool_start":
                assert agent.steer(_steer("use sqlite", "c1")) is True
                assert agent.steer("no id here") is True
                assert agent.steer(_steer("and keep the tests", "c2")) is True
                assert [_texts(steer) for steer in agent.pending_steers()] == [
                    ["use sqlite"],
                    ["no id here"],
                    ["and keep the tests"],
                ]

    delivered = [event.data["message"] for event in events if event.type == "user_message"]
    assert len(delivered) == 1
    merged = delivered[0]
    assert _texts(merged) == ["use sqlite", "no id here", "and keep the tests"]
    assert merged["meta"]["input_ids"] == ["c1", "c2"]
    assert merged["meta"]["steer"] is True
    assert agent.messages[3] == merged


async def test_take_steers_removes_them_before_delivery(tmp_path: Path) -> None:
    adapter = _Adapter([_ping_turn(), _text_turn("ok")])
    agent = _new_agent(tmp_path)

    taken: list[ConversationMessage] = []
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("go"):
            if event.type == "tool_start":
                agent.steer(_steer("first", "c1"))
                taken = agent.take_steers()

    assert [_texts(steer) for steer in taken] == [["first"]]
    assert [message["role"] for message in agent.messages] == ["user", "assistant", "user", "assistant"]


async def test_stop_refuses_new_steers_and_never_delivers_pending_ones(tmp_path: Path) -> None:
    adapter = _Adapter([_ping_turn(), _text_turn("unreachable")])
    agent = _new_agent(tmp_path)

    events: list[Event] = []
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("go"):
            events.append(event)
            if event.type == "tool_start":
                assert agent.steer("early") is True
                agent.cancel()
                assert agent.steer("late") is False

    assert events[-1] == Event("cancelled")
    assert "user_message" not in [event.type for event in events]
    assert len(adapter.requests) == 1
    assert all(not (message.get("meta") or {}).get("steer") for message in agent.messages)


async def test_cancel_during_the_steer_commit_reports_the_delivered_message_first(tmp_path: Path) -> None:
    adapter = _Adapter([_ping_turn(), _text_turn("unreachable")])
    agent = _new_agent(tmp_path)

    async def on_persist(message: ConversationMessage) -> None:
        if (message.get("meta") or {}).get("steer"):
            agent.cancel()
            await asyncio.sleep(0)

    events: list[Event] = []
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for event in agent.achat("go", on_persist=on_persist):
            events.append(event)
            if event.type == "tool_start":
                agent.steer("steer")

    assert [event.type for event in events][-2:] == ["user_message", "cancelled"]
    assert agent.messages[-1]["meta"]["steer"] is True
    assert len(adapter.requests) == 1


async def test_compact_operation_refuses_steers(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path, messages=[], session_dir=None)
    agent.messages = [
        {"role": "user", "content": [text_block("hello")]},
        {"role": "assistant", "content": [text_block("hi")]},
    ]
    accepted: list[bool] = []
    adapter = _Adapter([_text_turn("summary")], on_request=lambda _: accepted.append(agent.steer("now")))

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        await agent.acompact()

    assert accepted == [False]
    assert agent.messages[-1]["role"] == "compact"


def test_steer_rejects_unsupported_images_and_non_user_messages(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path, supports_image_input=False)
    image: ConversationMessage = {
        "role": "user",
        "content": [image_block("aGk=", mime_type="image/png")],
    }

    with pytest.raises(ValueError, match="does not support image input"):
        agent.steer(image)
    with pytest.raises(ValueError, match="must be a user message"):
        agent.steer({"role": "assistant", "content": [text_block("hi")]})
    assert agent.steer("idle") is False


def test_usage_restarts_at_the_steer_message(tmp_path: Path) -> None:
    started_at = "2020-01-01T00:00:00+00:00"
    agent = _new_agent(tmp_path)
    agent.model_pricing = {"input": 1.0, "output": 2.0}

    def steer_on_second_request(request_number: int) -> None:
        if request_number == 2:
            agent.steer("steer")

    adapter = _Adapter(
        [
            _ping_turn(usage={"total_tokens": 100, "input_tokens": 90, "output_tokens": 10}),
            _text_turn("first", usage={"total_tokens": 150, "input_tokens": 130, "output_tokens": 20}),
            _text_turn("second", usage={"total_tokens": 200, "input_tokens": 170, "output_tokens": 30}),
        ],
        on_request=steer_on_second_request,
    )

    user: ConversationMessage = {"role": "user", "content": [text_block("go")], "meta": {"created_at": started_at}}
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        result = agent.run(user)

    usages = [event.data for event in result.events if event.type == "usage"]
    assert len(usages) == 4
    assert usages[1]["turn_usage"] == {"total_tokens": 250, "input_tokens": 220, "output_tokens": 30}
    steer, answer = agent.messages[-2:]
    assert steer["meta"]["steer"] is True
    # The closing event repeats the segment's totals and runs to the steer message.
    closing_index = next(i for i, event in enumerate(result.events) if event.type == "user_message") - 1
    assert result.events[closing_index].type == "usage"
    closing = result.events[closing_index].data
    assert closing == {**usages[1], "turn_duration_ms": closing["turn_duration_ms"]}
    segment = datetime.fromisoformat(steer["meta"]["created_at"]) - datetime.fromisoformat(started_at)
    assert closing["turn_duration_ms"] == int(segment.total_seconds() * 1000)
    # The segment opened by the steer counts only its own request.
    last = usages[3]
    assert last["context_tokens"] == 200
    assert last["turn_usage"] == {"total_tokens": 200, "input_tokens": 170, "output_tokens": 30}
    assert last["turn_cost"] == estimate_cost(last["turn_usage"], agent.model_pricing)
    elapsed = datetime.fromisoformat(answer["meta"]["created_at"]) - datetime.fromisoformat(steer["meta"]["created_at"])
    assert last["turn_duration_ms"] == int(elapsed.total_seconds() * 1000)
    assert result.usage == last
