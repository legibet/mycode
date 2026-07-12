"""Tests for the workspace files listing endpoint."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from mycode.models import ModelMetadata
from mycode_cli.server.app import create_api_app


def _client() -> TestClient:
    return TestClient(create_api_app())


def test_files_lists_completion_candidates(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".env").write_text("K=V", encoding="utf-8")
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 rest")

    with _client() as client:
        body = client.get("/api/workspaces/files", params={"cwd": str(tmp_path)}).json()
        filtered = client.get(
            "/api/workspaces/files",
            params={"cwd": str(tmp_path), "prefix": "pi"},
        ).json()

    assert [(entry["path"], entry["kind"]) for entry in body["entries"]] == [
        ("src/", "directory"),
        (".env", "text"),
        ("doc.pdf", "document"),
        ("note.txt", "text"),
        ("pic.png", "image"),
    ]
    assert filtered["entries"] == [{"name": "pic.png", "path": "pic.png", "kind": "image"}]


def test_files_caps_at_limit_and_flags_truncated(tmp_path: Path) -> None:
    for i in range(150):
        (tmp_path / f"f{i:03d}.py").write_text("x", encoding="utf-8")

    with _client() as client:
        body = client.get("/api/workspaces/files", params={"cwd": str(tmp_path)}).json()

    assert len(body["entries"]) == 100
    assert body["truncated"] is True


def test_workspace_symlink_cannot_attach_file_outside_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    (workspace / "link.png").symlink_to(outside)

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

    with _client() as client:
        listing = client.get("/api/workspaces/files", params={"cwd": str(workspace)}).json()
        response = client.post(
            "/api/chat",
            json={
                "cwd": str(workspace),
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "input": [
                    {
                        "type": "image",
                        "path": "link.png",
                        "is_attachment": True,
                    }
                ],
            },
        )

    assert listing["entries"] == []
    assert response.status_code == 400
    assert response.json()["detail"] == "path outside workspace: link.png"
