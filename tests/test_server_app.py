"""Tests for FastAPI app startup behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.routing import Mount
from starlette.testclient import TestClient

from mycode.models import ModelMetadata
from mycode.session import SessionStore
from mycode_cli.server.app import create_api_app, create_app
from mycode_cli.server.deps import get_run_manager, get_store
from mycode_cli.server.run_manager import RunManager


def mount_paths(app) -> list[str]:
    return [route.path for route in app.routes if isinstance(route, Mount)]


@pytest.mark.parametrize(
    ("factory", "has_static", "expected_mounts"),
    [
        (create_app, True, [""]),
        (create_app, False, []),
        (create_api_app, True, []),
    ],
)
def test_web_mount_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
    has_static: bool,
    expected_mounts: list[str],
) -> None:
    static_dir = tmp_path if has_static else tmp_path / "missing"
    if has_static:
        (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr("mycode_cli.server.app.web_static_path", lambda: static_dir)

    app = factory()

    assert mount_paths(app) == expected_mounts


def test_create_app_starts_without_models_catalog_side_effects() -> None:
    with TestClient(create_app(serve_web=False)):
        pass


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
    assert not store.session_dir("new-session").exists()
