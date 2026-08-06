"""Tests for FastAPI app behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mycode.messages import ConversationMessage
from mycode.models import ModelMetadata
from mycode.providers.base import ProviderRequest, ProviderStreamEvent
from mycode.session import SessionStore
from mycode_cli.server.app import create_api_app, create_app
from mycode_cli.server.deps import get_run_manager, get_store
from mycode_cli.server.run_manager import RunManager


class _CaptureAdapter:
    supports_reasoning_effort = False

    def __init__(self) -> None:
        self.messages: list[ConversationMessage] | None = None
        self.reasoning_effort: str | None = None

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.messages = list(request.messages)
        self.reasoning_effort = request.reasoning_effort
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        )


# App serving and CORS


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
            supports_reasoning=True,
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
    assert store.load_session_sync("new-session") is None


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


@pytest.mark.parametrize(
    ("include_effort", "effort", "expected_effort"),
    [(False, None, "high"), (True, "auto", None), (True, None, None)],
)
def test_model_effort_config_and_request_override(
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
                        "reasoning_effort": "high",
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
        await store.append_message(session_id, {"role": "assistant", "content": [{"type": "text", "text": "hi"}]})

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
        assert [event["type"] for event in events] == ["compact"]

        session = client.get("/api/sessions/s1").json()

        # Immediately repeating compact finds no new context.
        repeat = client.post("/api/sessions/s1/compact", json={})
        assert repeat.status_code == 400
        assert repeat.json()["detail"] == "nothing to compact"

    roles = [message["role"] for message in session["messages"]]
    assert roles == ["user", "assistant", "compact"]
    assert session["messages"][-1]["content"][0]["text"] == "ok"
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


def test_session_load_stamps_request_costs(tmp_path: Path) -> None:
    store = SessionStore(data_dir=tmp_path / "sessions")

    async def seed() -> None:
        await store.create_session("s1", cwd=str(tmp_path))
        await store.append_message("s1", {"role": "user", "content": [{"type": "text", "text": "hi"}]})
        await store.append_message(
            "s1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "meta": {"provider": "p", "model": "m", "usage": {"cost_usd": 0.02}},
            },
        )
        # Usage recorded but the model has no catalog price: no stamp, and
        # the session total stays unknown.
        await store.append_message(
            "s1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "meta": {"provider": "unknown", "model": "unknown", "usage": {"input_tokens": 10, "output_tokens": 5}},
            },
        )

    asyncio.run(seed())
    app = create_api_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_run_manager] = lambda: RunManager()

    with TestClient(app) as client:
        payload = client.get("/api/sessions/s1").json()

    user_message, priced, unpriced = payload["messages"]
    assert "request_cost_usd" not in (user_message.get("meta") or {})
    assert priced["meta"]["request_cost_usd"] == pytest.approx(0.02)
    assert "request_cost_usd" not in unpriced["meta"]
    assert payload["session_cost_usd"] is None
