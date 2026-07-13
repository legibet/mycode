"""Tests for FastAPI app behavior."""

from __future__ import annotations

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

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.messages = list(request.messages)
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        )


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
        "mycode_cli.server.routers.chat.resolve_model_metadata",
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
    monkeypatch.setattr("mycode_cli.server.routers.chat.get_provider_adapter", lambda _provider: adapter)
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
