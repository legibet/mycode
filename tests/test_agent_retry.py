"""Timeout and retry behavior of the provider attempt runner."""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from mycode import Agent, Event, ProviderError, SessionStore
from mycode.providers.base import ProviderStreamEvent, normalize_provider_error


def _text_turn(text: str = "done") -> list[ProviderStreamEvent]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "meta": {"usage": {"total_tokens": 10, "input_tokens": 6, "output_tokens": 4}},
    }
    return [
        ProviderStreamEvent("text_delta", {"text": text}),
        ProviderStreamEvent("message_done", {"message": message}),
    ]


def _transient_error(message: str = "upstream down", **overrides: Any) -> ProviderError:
    # retry_after doubles as a fast, deterministic backoff for tests.
    overrides.setdefault("reason", "connection_error")
    overrides.setdefault("retryable", True)
    overrides.setdefault("retry_after", 0.01)
    return ProviderError(message, **overrides)


class _ScriptedAdapter:
    """Raises the scripted errors first, then streams the scripted turn."""

    def __init__(self, errors: list[ProviderError] | None = None, turn: list[ProviderStreamEvent] | None = None):
        self._errors = list(errors or [])
        self._turn = list(turn or [])
        self.attempts = 0

    async def stream_turn(self, _request: Any):
        self.attempts += 1
        if self._errors:
            raise self._errors.pop(0)
        yield ProviderStreamEvent("stream_started")
        for event in self._turn:
            yield event


class _SilentFirstAdapter:
    """Hangs before the first event on attempt one, then answers normally."""

    def __init__(self, turn: list[ProviderStreamEvent]):
        self._turn = list(turn)
        self.attempts = 0

    async def stream_turn(self, _request: Any):
        self.attempts += 1
        if self.attempts == 1:
            await asyncio.sleep(60)
        yield ProviderStreamEvent("stream_started")
        for event in self._turn:
            yield event


class _FailsAfterTextAdapter:
    def __init__(self):
        self.attempts = 0

    async def stream_turn(self, _request: Any):
        self.attempts += 1
        yield ProviderStreamEvent("stream_started")
        yield ProviderStreamEvent("text_delta", {"text": "partial"})
        raise _transient_error("stream died mid-turn")


class _FailsAfterReasoningAdapter:
    def __init__(self):
        self.attempts = 0

    async def stream_turn(self, _request: Any):
        self.attempts += 1
        yield ProviderStreamEvent("stream_started")
        yield ProviderStreamEvent("thinking_delta", {"text": "thinking"})
        raise _transient_error("reasoning stream died")


def _new_agent(tmp_path: Path, **overrides: Any) -> Agent:
    overrides.setdefault("model", "gpt-5.5")
    overrides.setdefault("cwd", str(tmp_path))
    overrides.setdefault("session_dir", tmp_path)
    overrides.setdefault("session_id", "session")
    return Agent(**overrides)


async def _collect(agent: Agent, prompt: str = "hi") -> list[Event]:
    return [event async for event in agent.achat(prompt)]


async def test_transient_failures_retry_until_success(tmp_path: Path) -> None:
    adapter = _ScriptedAdapter(
        errors=[
            _transient_error("overloaded", reason="http_status", status_code=529),
            # An excessive Retry-After falls back to computed backoff.
            _transient_error("still overloaded", retry_after=120.0),
        ],
        turn=_text_turn("recovered"),
    )
    agent = _new_agent(tmp_path)
    agent.model_pricing = {"input": 1.0, "output": 2.0}

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    retries = [event for event in events if event.type == "retry"]
    assert [(r.data["attempt"], r.data["max_attempts"]) for r in retries] == [(2, 3), (3, 3)]
    assert retries[0].data["reason"] == "http_status"
    assert retries[0].data["status_code"] == 529
    assert retries[0].data["message"] == "overloaded"
    assert retries[1].data["reason"] == "connection_error"
    assert 0 < retries[1].data["delay_seconds"] <= 8
    assert adapter.attempts == 3
    assert any(event.type == "text" and event.data["delta"] == "recovered" for event in events)
    assert not any(event.type == "error" for event in events)

    usage = [event for event in events if event.type == "usage"][-1]
    assert usage.data["turn_usage"] == {"total_tokens": 10, "input_tokens": 6, "output_tokens": 4}
    assert usage.data["turn_cost"] == {
        "input": 6 / 1_000_000,
        "output": 8 / 1_000_000,
        "total": 14 / 1_000_000,
    }
    assert usage.data["context_tokens"] == 10


async def test_non_retryable_failure_fails_immediately(tmp_path: Path) -> None:
    adapter = _ScriptedAdapter(errors=[ProviderError("invalid request", reason="http_status", status_code=400)])
    agent = _new_agent(tmp_path)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    assert adapter.attempts == 1
    assert not any(event.type == "retry" for event in events)
    assert events[-1] == Event("error", {"message": "invalid request"})


async def test_exhausted_retries_surface_last_error_and_persist_nothing(tmp_path: Path) -> None:
    adapter = _ScriptedAdapter(errors=[_transient_error(f"down {i}") for i in range(5)])
    agent = _new_agent(tmp_path, max_retries=2)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    assert adapter.attempts == 3
    assert len([event for event in events if event.type == "retry"]) == 2
    assert events[-1] == Event("error", {"message": "down 2"})

    # No attempt produced output — nothing but the user message is persisted.
    assert [message["role"] for message in agent.messages] == ["user"]


async def test_stream_start_timeout_cancels_and_retries(tmp_path: Path) -> None:
    adapter = _SilentFirstAdapter(_text_turn("late but fine"))
    agent = _new_agent(tmp_path, stream_start_timeout=0.05, max_retries=1)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    retries = [event for event in events if event.type == "retry"]
    assert len(retries) == 1
    assert retries[0].data["reason"] == "stream_start_timeout"
    assert adapter.attempts == 2
    assert any(event.type == "text" and event.data["delta"] == "late but fine" for event in events)


async def test_stream_start_timeout_exhausted_raises_dedicated_error(tmp_path: Path) -> None:
    adapter = _SilentFirstAdapter(_text_turn())
    agent = _new_agent(tmp_path, stream_start_timeout=0.05, max_retries=0)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    assert adapter.attempts == 1
    assert events[-1].type == "error"
    assert "no provider stream event received within" in events[-1].data["message"]


async def test_no_retry_after_output_and_partial_persisted_once(tmp_path: Path) -> None:
    adapter = _FailsAfterTextAdapter()
    agent = _new_agent(tmp_path)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    assert adapter.attempts == 1
    assert not any(event.type == "retry" for event in events)
    assert events[-1] == Event("error", {"message": "stream died mid-turn"})

    data = SessionStore(data_dir=tmp_path).load_session_sync("session")
    assert data is not None
    assistants = [message for message in data["messages"] if message["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == [{"type": "text", "text": "partial"}]
    assert assistants[0]["meta"]["stop_reason"] == "error"


async def test_no_retry_after_reasoning_and_partial_persisted_once(tmp_path: Path) -> None:
    adapter = _FailsAfterReasoningAdapter()
    agent = _new_agent(tmp_path)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    assert adapter.attempts == 1
    assert not any(event.type == "retry" for event in events)
    assert events[-1] == Event("error", {"message": "reasoning stream died"})
    assistants = [message for message in agent.messages if message["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == [
        {"type": "thinking", "text": "thinking", "meta": {"duration_ms": pytest.approx(0, abs=1000)}}
    ]
    assert assistants[0]["meta"]["stop_reason"] == "error"


async def test_cancel_interrupts_backoff_and_honors_retry_after(tmp_path: Path) -> None:
    adapter = _ScriptedAdapter(
        errors=[_transient_error("rate limited", reason="http_status", status_code=429, retry_after=30.0)],
        turn=_text_turn(),
    )
    agent = _new_agent(tmp_path)
    events: list[Event] = []

    async def run() -> None:
        async for event in agent.achat("hi"):
            events.append(event)
            if event.type == "retry":
                agent.cancel()

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        # Fails within the timeout only if cancel() does not interrupt the
        # 30s Retry-After backoff.
        await asyncio.wait_for(run(), timeout=5)

    retries = [event for event in events if event.type == "retry"]
    assert retries[0].data["delay_seconds"] == 30.0
    assert adapter.attempts == 1
    assert events[-1] == Event("error", {"message": "cancelled"})


async def test_failure_after_stream_start_but_before_output_still_retries(tmp_path: Path) -> None:
    class _DiesMidHandshakeAdapter:
        """Stream starts (e.g. mid tool-call arguments) but dies before output."""

        def __init__(self, turn: list[ProviderStreamEvent]):
            self._turn = list(turn)
            self.attempts = 0

        async def stream_turn(self, _request: Any):
            self.attempts += 1
            yield ProviderStreamEvent("stream_started")
            if self.attempts == 1:
                raise _transient_error("died before any output")
            for event in self._turn:
                yield event

    adapter = _DiesMidHandshakeAdapter(_text_turn("second try"))
    agent = _new_agent(tmp_path)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = await _collect(agent)

    assert adapter.attempts == 2
    assert len([event for event in events if event.type == "retry"]) == 1
    assert any(event.type == "text" and event.data["delta"] == "second try" for event in events)


async def test_acompact_retries_and_keeps_successful_marker_usage(tmp_path: Path) -> None:
    class _FlakySummaryAdapter:
        def __init__(self):
            self.attempts = 0

        async def stream_turn(self, _request: Any):
            self.attempts += 1
            if self.attempts == 2:  # request 1 is the chat turn, 2+ the summary
                raise _transient_error("summary attempt lost")
            yield ProviderStreamEvent("stream_started")
            text = "chat reply" if self.attempts == 1 else "THE_SUMMARY"
            message: dict[str, Any] = {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "meta": {"usage": {"total_tokens": 42}},
            }
            yield ProviderStreamEvent("message_done", {"message": message})

    adapter = _FlakySummaryAdapter()
    agent = _new_agent(tmp_path, compact_threshold=0)

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        async for _ in agent.achat("hello"):
            pass
        marker = await agent.acompact()

    assert adapter.attempts == 3
    assert marker["content"][0]["text"] == "THE_SUMMARY"
    assert marker["meta"]["usage"] == {"total_tokens": 42}


def test_normalized_provider_errors_are_readable_and_classified() -> None:
    # str(httpx.ReadTimeout("")) is empty; the incident surfaced it as a blank error.
    error = normalize_provider_error(httpx.ReadTimeout(""), "openai")
    assert error.retryable
    assert error.reason == "request_timeout"
    assert str(error) == "ReadTimeout while streaming from openai"

    class _FakeStatusError(Exception):
        """Duck-typed shape shared by openai/anthropic APIStatusError."""

        def __init__(self) -> None:
            super().__init__("rate limited")
            self.status_code = 429
            self.response = httpx.Response(429, headers={"retry-after": "7"})

    error = normalize_provider_error(_FakeStatusError(), "openai")
    assert error.retryable
    assert error.reason == "http_status"
    assert error.status_code == 429
    assert error.retry_after == 7.0
