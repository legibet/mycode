"""Tests for FastAPI app behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from threading import Event as ThreadEvent
from typing import cast, override

import httpx2
import pytest
from fastapi import Request
from starlette.testclient import TestClient

from mycode.agent import Event
from mycode.messages import ConversationMessage
from mycode.models import ModelMetadata
from mycode.providers.base import ProviderRequest, ProviderStreamEvent
from mycode.session import SessionStore as TimelineStore
from mycode_cli.server.app import create_api_app, create_app
from mycode_cli.server.deps import get_run_manager, get_store
from mycode_cli.server.run_manager import RunManager
from mycode_cli.sessions import SessionStore


class _CaptureAdapter:
    supports_reasoning_effort = False

    def __init__(self) -> None:
        self.messages: list[ConversationMessage] | None = None
        self.reasoning_effort: str | None = None
        self.legacy_max_tokens = False

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.messages = list(request.messages)
        self.reasoning_effort = request.reasoning_effort
        self.legacy_max_tokens = request.legacy_max_tokens
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        )


# App serving and CORS


def test_app_lifespans_isolate_resources_and_persist_cancelled_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = ThreadEvent()

    class PartialAdapter(_CaptureAdapter):
        @override
        async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
            yield ProviderStreamEvent("text_delta", {"text": "partial reply"})
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: PartialAdapter())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "first"))
    first_app = create_app(serve_web=False)
    second_app = create_app(serve_web=False)
    with TestClient(first_app) as first:
        response = first.post(
            "/api/chat",
            json={"session_id": "s1", "cwd": str(tmp_path), "provider": "anthropic", "message": "hello"},
        )
        assert response.status_code == 200, response.text
        assert started.wait(2)
        runs = cast(RunManager, first_app.state.runs)
        store = cast(SessionStore, first_app.state.store)
        assert first.portal is not None
        state = first.portal.call(runs.get_run, response.json()["run"]["id"])
        assert state is not None
        assert state.task is not None

        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "second"))
        with TestClient(second_app) as second:
            assert second_app.state.store is not store
            assert second_app.state.runs is not runs
            assert second.get("/api/sessions").json()["sessions"] == []
            assert [session["id"] for session in first.get("/api/sessions").json()["sessions"]] == ["s1"]
        assert state.status == "running"

    assert state.task.done()
    assert state.status == "cancelled"
    messages = store.load_raw_messages_sync("s1")
    assert messages[-1]["meta"]["stop_reason"] == "cancelled"
    assert messages[-1]["content"][0]["text"] == "partial reply"


@pytest.mark.parametrize(
    ("factory", "has_static", "expected_status"),
    [
        (create_app, True, 200),
        (create_app, False, 404),
        (create_api_app, True, 404),
    ],
)
def test_web_root_serves_packaged_assets_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
    has_static: bool,
    expected_status: int,
) -> None:
    static_dir = tmp_path if has_static else tmp_path / "missing"
    if has_static:
        (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr("mycode_cli.server.app.web_static_path", lambda: static_dir)

    with TestClient(factory()) as client:
        response = client.get("/")

    assert response.status_code == expected_status


def test_packaged_web_app_does_not_enable_cors_by_default() -> None:
    with TestClient(create_app(serve_web=False)) as client:
        response = client.options(
            "/api/settings",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert "access-control-allow-origin" not in response.headers


def test_api_dev_app_allows_only_local_vite_cors() -> None:
    with TestClient(create_api_app()) as client:
        allowed = client.options(
            "/api/settings",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = client.options(
            "/api/settings",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


# Chat API


@pytest.mark.parametrize("history", ["new", "empty", "timeline-only", "existing", "rewind"])
def test_chat_reads_only_required_history_and_preserves_replay_and_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    adapter = _CaptureAdapter()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: adapter)
    store = SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: runs
    has_timeline = history in {"timeline-only", "existing", "rewind"}

    async def seed() -> None:
        if history in {"empty", "existing", "rewind"}:
            await store.create_session("s1", cwd=str(tmp_path))
        if has_timeline:
            await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "discarded"}]})
            await store.append_message("s1", {"role": "assistant", "meta": {"cost": {"total": 0.25}}})
            await store.append_rewind("s1", 0)
            await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "kept"}]})
            await store.append_message(
                "s1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "answer"}],
                    "meta": {"cost": {"total": 0.5}},
                },
            )

    asyncio.run(seed())
    reads = 0
    original_load = TimelineStore.load_raw_messages_sync

    def load(timeline: TimelineStore, session_id: str) -> list[ConversationMessage]:
        nonlocal reads
        reads += 1
        return original_load(timeline, session_id)

    monkeypatch.setattr(TimelineStore, "load_raw_messages_sync", load)
    payload: dict[str, object] = {
        "session_id": "s1",
        "cwd": str(tmp_path),
        "message": "follow-up",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }
    if history == "rewind":
        payload["rewind_to"] = 0
    with TestClient(app) as client:
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 200, response.text
        assert reads == (3 if history == "rewind" else 2)
        run_id = response.json()["run"]["id"]
        with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
            events = [json.loads(line[6:]) for line in stream.iter_lines() if line.startswith("data: {")]

    assert adapter.messages is not None
    texts = [block["text"] for message in adapter.messages for block in message.get("content") or []]
    assert texts == (["kept", "answer"] if has_timeline and history != "rewind" else []) + ["follow-up"]
    usage = [event for event in events if event["type"] == "usage"]
    assert usage
    assert usage[-1].get("session_cost") == (0.75 if has_timeline else None)


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hi", "input": [{"type": "text", "text": "also hi"}]},
        {"message": "   "},
        {"input": [{"type": "image", "data": "abc"}]},
        {"input": [{"type": "document", "data": "abc", "mime_type": "text/plain"}]},
        {"input": [{"type": "image"}]},
        {"input": [{"type": "text", "text": "x", "path": "a.py", "is_attachment": True}]},
        {"input": [{"type": "text", "path": "a.py"}]},
    ],
)
def test_chat_request_shape_validation(payload: dict[str, object]) -> None:
    with TestClient(create_api_app()) as client:
        response = client.post("/api/chat", json=payload)

    assert response.status_code == 422


def test_chat_capability_failure_does_not_create_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "mycode_cli.server.routers.chat.resolve_configured_model_metadata",
        lambda **_: ModelMetadata(
            provider="anthropic",
            model="claude-sonnet-4-6",
            supports_image_input=False,
            supports_pdf_input=True,
        ),
    )
    store = SessionStore(data_dir=tmp_path / "sessions")
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: RunManager()

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": "new-session",
                "cwd": str(tmp_path),
                "message": None,
                "input": [{"type": "image", "data": "abc", "mime_type": "image/png"}],
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "current model does not support image input"
    assert asyncio.run(store.load_session("new-session")) is None


def test_chat_skill_reference_reaches_provider_and_keeps_visible_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home"))
    skill_dir = tmp_path / ".mycode" / "skills" / "ui"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: ui\ndescription: Design interfaces.\n---\n\nReview the interface carefully.\n",
        encoding="utf-8",
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: adapter)
    store = SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: runs
    prompt = "Use /ui to polish this page"

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": "skill-session",
                "cwd": str(tmp_path),
                "message": prompt,
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run"]["id"]
        with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
            list(stream.iter_lines())
        session = client.get("/api/sessions/skill-session").json()

    assert adapter.messages is not None
    user_blocks = adapter.messages[0]["content"]
    assert "Review the interface carefully." in user_blocks[0]["text"]
    assert "description: Design interfaces." not in user_blocks[0]["text"]
    assert user_blocks[-1]["text"] == prompt
    assert session["session"]["title"] == prompt


@pytest.mark.parametrize(
    ("opt_in", "effort", "expected_status"),
    [(True, "low", 200), (False, "low", 400), (True, "high", 400)],
)
def test_chat_rejects_unsupported_reasoning_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opt_in: bool,
    effort: str,
    expected_status: int,
) -> None:
    home = tmp_path / "home" / ".mycode"
    home.mkdir(parents=True)
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(home))
    home.joinpath("config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "custom": {
                        "type": "openai_chat",
                        "api_key": "${XAI_API_KEY}",
                        "base_url": "https://api.x.ai/v1",
                        "supports_reasoning_effort": opt_in,
                        "models": {"grok-4.5": {"reasoning_efforts": ["low"]}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: adapter)
    store = SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: runs

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": "effort-session",
                "cwd": str(tmp_path),
                "provider": "custom",
                "model": "grok-4.5",
                "message": "hi",
                "reasoning_effort": effort,
            },
        )
        assert response.status_code == expected_status
        if expected_status == 200:
            run_id = response.json()["run"]["id"]
            with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
                list(stream.iter_lines())
        else:
            assert "reasoning effort" in response.json()["detail"]

    if expected_status == 200:
        assert adapter.reasoning_effort == "low"


def test_chat_legacy_max_tokens_reaches_provider_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home" / ".mycode"
    home.mkdir(parents=True)
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(home))
    home.joinpath("config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "custom": {
                        "type": "openai_chat",
                        "api_key": "${TEST_API_KEY}",
                        "base_url": "https://compat.example/v1",
                        "legacy_max_tokens": True,
                        "models": {"some-model": {}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: adapter)
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app.dependency_overrides[get_run_manager] = lambda: runs

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": "legacy-max-tokens-session",
                "cwd": str(tmp_path),
                "provider": "custom",
                "model": "some-model",
                "message": "hi",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run"]["id"]
        with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
            list(stream.iter_lines())

    assert adapter.legacy_max_tokens is True


@pytest.mark.parametrize(
    ("include_effort", "effort", "expected_effort"),
    [(False, None, None), (True, "auto", None), (True, None, None)],
)
def test_model_effort_request_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_effort: bool,
    effort: str | None,
    expected_effort: str | None,
) -> None:
    home = tmp_path / "home" / ".mycode"
    home.mkdir(parents=True)
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(home))
    home.joinpath("config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "custom": {
                        "type": "openai_chat",
                        "api_key": "${XAI_API_KEY}",
                        "supports_reasoning_effort": True,
                        "models": {"custom-model": {"reasoning_efforts": ["low", "high"]}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: adapter)
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app.dependency_overrides[get_run_manager] = lambda: runs

    with TestClient(app) as client:
        provider_info = client.get("/api/config", params={"cwd": str(tmp_path)}).json()["providers"]["custom"]
        assert provider_info["reasoning_efforts"] == {"custom-model": ["low", "high"]}

        payload: dict[str, object] = {
            "session_id": "effort-config-session",
            "cwd": str(tmp_path),
            "provider": "custom",
            "model": "custom-model",
            "message": "hi",
        }
        if include_effort:
            payload["reasoning_effort"] = effort
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 200
        run_id = response.json()["run"]["id"]
        with client.stream("GET", f"/api/runs/{run_id}/stream") as stream:
            list(stream.iter_lines())

    assert adapter.reasoning_effort == expected_effort


# Compact API


def _seed_session(store: SessionStore, session_id: str, cwd: str) -> None:
    async def seed() -> None:
        await store.create_session(session_id, cwd=cwd)
        await store.append_message(session_id, {"role": "user", "content": [{"type": "text", "text": "hello"}]})
        await store.append_message(
            session_id,
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}], "meta": {"cost": {"total": 0.25}}},
        )

    asyncio.run(seed())


def test_compact_endpoint_persists_marker_without_synthetic_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home"))
    adapter = _CaptureAdapter()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: adapter)
    store = SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: runs
    _seed_session(store, "s1", str(tmp_path))

    with TestClient(app) as client:
        missing = client.post("/api/sessions/missing/compact", json={})
        assert missing.status_code == 404

        response = client.post(
            "/api/sessions/s1/compact",
            json={"provider": "anthropic", "model": "claude-sonnet-4-6"},
        )
        assert response.status_code == 200
        run = response.json()["run"]
        assert run["kind"] == "compact"
        assert run["status"] == "running"

        with client.stream("GET", f"/api/runs/{run['id']}/stream") as stream:
            events = [
                json.loads(line[6:])
                for line in stream.iter_lines()
                if line.startswith("data:") and line != "data: [DONE]"
            ]
        assert [(event["type"], event["trigger"]) for event in events] == [("compact", "manual")]

        session = client.get("/api/sessions/s1").json()

        # Immediately repeating compact finds no new context.
        repeat = client.post("/api/sessions/s1/compact", json={})
        assert repeat.status_code == 400
        assert repeat.json()["detail"] == "nothing to compact"

    roles = [message["role"] for message in session["messages"]]
    assert roles == ["user", "assistant", "compact"]
    assert session["messages"][-1]["content"][0]["text"] == "ok"
    assert session["messages"][-1]["meta"]["trigger"] == "manual"
    assert session["active_run"] is None
    # The summary request replayed the seeded history for the requested model.
    assert adapter.messages is not None
    assert adapter.messages[0]["content"][0]["text"] == "hello"


def test_compact_endpoint_conflicts_and_cancel_write_no_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home"))

    class _HangingAdapter:
        supports_reasoning_effort = False

        async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
            del request
            await asyncio.sleep(30)
            yield ProviderStreamEvent("message_done", {"message": {}})

    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: _HangingAdapter())
    store = SessionStore(data_dir=tmp_path / "sessions")
    runs = RunManager()
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: runs
    _seed_session(store, "s1", str(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/sessions/s1/compact", json={})
        assert response.status_code == 200
        run = response.json()["run"]

        # While compacting: reconnect sees the compact run without an optimistic
        # turn, and both compact and chat starts conflict.
        session = client.get("/api/sessions/s1").json()
        assert session["active_run"]["kind"] == "compact"
        assert session["session_cost"] == 0.25
        assert [message["role"] for message in session["messages"]] == ["user", "assistant"]

        conflict = client.post("/api/sessions/s1/compact", json={})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["run"]["id"] == run["id"]

        chat_conflict = client.post("/api/chat", json={"session_id": "s1", "message": "hi"})
        assert chat_conflict.status_code == 409

        cancelled = client.post(f"/api/runs/{run['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["run"]["status"] == "cancelled"

        session = client.get("/api/sessions/s1").json()

    assert [message["role"] for message in session["messages"]] == ["user", "assistant"]


# Sessions API


def test_session_load_returns_persisted_costs_from_one_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(data_dir=tmp_path / "sessions")

    async def seed() -> None:
        await store.create_session("s1", cwd=str(tmp_path))
        await store.append_message("s1", {"role": "compact", "meta": {"cost": {"total": 0.03}}})
        await store.append_rewind("s1", 0)
        await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "hi"}]})
        await store.append_message(
            "s1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "meta": {
                    "provider": "p",
                    "model": "m",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "cost": {"input": 0.01, "output": 0.01, "total": 0.02},
                },
            },
        )
        await store.append_message(
            "s1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "meta": {"provider": "unknown", "model": "unknown", "usage": {"input_tokens": 10, "output_tokens": 5}},
            },
        )

    asyncio.run(seed())
    reads = 0
    original_load = store.load_raw_messages_sync

    def load(session_id: str) -> list[ConversationMessage]:
        nonlocal reads
        reads += 1
        return original_load(session_id)

    monkeypatch.setattr(store, "load_raw_messages_sync", load)
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: RunManager()

    with TestClient(app) as client:
        payload = client.get("/api/sessions/s1").json()
        assert reads == 1
        assert client.get("/api/sessions/missing").json()["session"] is None
        assert reads == 1

    user_message, priced, unpriced = payload["messages"]
    assert "cost" not in (user_message.get("meta") or {})
    assert priced["meta"]["cost"] == pytest.approx({"input": 0.01, "output": 0.01, "total": 0.02})
    assert "cost" not in unpriced["meta"]
    assert payload["session_cost"] == pytest.approx(0.05)


async def test_running_session_uses_snapshot_cost_after_usage_eviction_then_loads_final_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("mycode_cli.server.run_manager.RUN_EVENT_BUFFER_SIZE", 1)
    app = create_api_app()
    async with app.router.lifespan_context(app):
        store = cast(SessionStore, app.state.store)
        runs = cast(RunManager, app.state.runs)
        await store.create_session("s1", cwd=str(tmp_path))
        await store.append_message("s1", {"role": "assistant", "meta": {"cost": {"total": 0.25}}})
        data = await store.load_session("s1")
        assert data is not None
        ready = asyncio.Event()
        release = asyncio.Event()

        class StreamingAgent:
            model = "test-model"
            context_window = 1000

            def cancel(self) -> None:
                release.set()

            async def acompact(self) -> ConversationMessage:
                raise NotImplementedError

            async def achat(self, user_input: str | ConversationMessage) -> AsyncGenerator[Event, None]:
                assert isinstance(user_input, dict)
                await store.append_message("s1", user_input)
                yield Event("usage", {"turn_cost": {"total": 0.5}})
                yield Event("text", {"delta": "answer"})
                ready.set()
                await release.wait()
                await store.append_message(
                    "s1",
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "answer"}],
                        "meta": {"cost": {"total": 0.5}},
                    },
                )

        run = await runs.start_run(
            session_id="s1",
            user_message={"role": "user", "content": [{"type": "text", "text": "question"}]},
            base_messages=data["messages"],
            session_cost_base=0.25,
            agent=StreamingAgent(),
        )
        await asyncio.wait_for(ready.wait(), 2)
        state = await runs.get_run(run["id"])
        assert state is not None
        assert state.task is not None
        reads = 0
        original_load = store.load_raw_messages_sync

        def load(session_id: str) -> list[ConversationMessage]:
            nonlocal reads
            reads += 1
            return original_load(session_id)

        monkeypatch.setattr(store, "load_raw_messages_sync", load)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test") as client:
            active = (await client.get("/api/sessions/s1")).json()
            assert reads == 0
            assert active["session_cost"] == 0.75
            assert active["active_run"]["id"] == run["id"]
            assert [event["type"] for event in active["pending_events"]] == ["text"]
            assert active["messages"][-1]["content"][0]["text"] == "question"

            release.set()
            await state.task
            finished = (await client.get("/api/sessions/s1")).json()
            assert reads == 1
            assert finished["active_run"] is None
            assert finished["session_cost"] == active["session_cost"]
            assert finished["messages"][-1]["content"][0]["text"] == "answer"


@pytest.mark.parametrize("operation", ["clear", "delete"])
async def test_session_read_serializes_with_catalog_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home"))
    app = create_api_app()
    async with app.router.lifespan_context(app):
        store = cast(SessionStore, app.state.store)
        runs = cast(RunManager, app.state.runs)
        await store.create_session("s1", cwd=str(tmp_path))
        await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "original"}]})
        reading = asyncio.Event()
        release_read = asyncio.Event()
        mutation_requested = asyncio.Event()
        mutation_started = asyncio.Event()
        original_load = store.load_session
        original_mutation = store.clear_session if operation == "clear" else store.delete_session

        async def load(session_id: str):
            reading.set()
            await release_read.wait()
            return await original_load(session_id)

        async def mutate(session_id: str) -> None:
            mutation_started.set()
            await original_mutation(session_id)

        async def manager(request: Request) -> RunManager:
            if request.method != "GET":
                mutation_requested.set()
            return runs

        monkeypatch.setattr(store, "load_session", load)
        monkeypatch.setattr(store, f"{operation}_session", mutate)
        app.dependency_overrides[get_run_manager] = manager
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test") as client:
            read = asyncio.create_task(client.get("/api/sessions/s1"))
            await asyncio.wait_for(reading.wait(), 2)
            mutation = asyncio.create_task(
                client.post("/api/sessions/s1/clear") if operation == "clear" else client.delete("/api/sessions/s1")
            )
            try:
                await asyncio.wait_for(mutation_requested.wait(), 2)
                assert not mutation_started.is_set()
            finally:
                release_read.set()
                before, changed = await asyncio.gather(read, mutation)
            assert before.json()["messages"][0]["content"][0]["text"] == "original"
            assert changed.status_code == 200
            after = (await client.get("/api/sessions/s1")).json()
            assert after["messages"] == []
            assert after["session_cost"] is None
