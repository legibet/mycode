"""Attachment behavior on Agent.achat() / Agent.run()."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mycode import Agent, Attachment
from mycode.providers.base import ProviderStreamEvent

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PDF = b"%PDF-1.4\n%fake\n"


class _Capture:
    def __init__(self) -> None:
        self.user_content: list[dict[str, Any]] = []

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        for message in reversed(request.messages):
            if message.get("role") == "user":
                self.user_content = list(message.get("content") or [])
                break
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        )


def _agent(tmp_path: Path, **overrides: Any) -> Agent:
    overrides.setdefault("model", "gpt-5.5")
    overrides.setdefault("cwd", str(tmp_path))
    overrides.setdefault("supports_image_input", True)
    overrides.setdefault("supports_pdf_input", True)
    return Agent(**overrides)


async def _send_attachments(agent: Agent, attachments: list[Any]) -> _Capture:
    cap = _Capture()
    with patch("mycode.agent.get_provider_adapter", return_value=cap):
        async for _ in agent.achat("go", attachments=attachments):
            pass
    return cap


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,payload,expected_type,expected_mime",
    [
        ("pic.png", PNG, "image", "image/png"),
        ("paper.pdf", PDF, "document", "application/pdf"),
    ],
)
async def test_path_attachment_picks_block_by_content(
    tmp_path: Path, filename: str, payload: bytes, expected_type: str, expected_mime: str
) -> None:
    (tmp_path / filename).write_bytes(payload)
    cap = await _send_attachments(_agent(tmp_path), [filename])
    block = cap.user_content[-1]
    assert block["type"] == expected_type
    assert block["mime_type"] == expected_mime


@pytest.mark.asyncio
async def test_text_path_attachment_is_visible_to_model(tmp_path: Path) -> None:
    (tmp_path / "n.txt").write_text("hello world", encoding="utf-8")
    cap = await _send_attachments(_agent(tmp_path), ["n.txt"])
    block = cap.user_content[-1]
    assert block["type"] == "text"
    assert "hello world" in block["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,media_type,expected_type",
    [
        (PNG, "image/png", "image"),
        (PDF, "application/pdf", "document"),
    ],
)
async def test_bytes_attachment_uses_declared_media_type(
    tmp_path: Path, data: bytes, media_type: str, expected_type: str
) -> None:
    cap = await _send_attachments(_agent(tmp_path), [Attachment.bytes(data, media_type=media_type)])
    assert cap.user_content[-1]["type"] == expected_type


@pytest.mark.asyncio
async def test_bad_attachment_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\xff\xfe")
    agent = _agent(tmp_path)
    bad_cases: list[tuple[Any, str]] = [
        ("missing.txt", "not found"),
        ("sub", "directory"),
        ("b.bin", "unsupported attachment"),
        (Attachment.bytes(b"x", media_type="application/zip"), "unsupported media_type"),
    ]
    for attachment, match in bad_cases:
        with pytest.raises(ValueError, match=match):
            async for _ in agent.achat("x", attachments=[attachment]):
                pass


def test_bytes_without_media_type_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="media_type"):
        Attachment.bytes(b"x", media_type="")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability,payload,error_word",
    [
        ("supports_image_input", PNG, "image"),
        ("supports_pdf_input", PDF, "PDF"),
    ],
)
async def test_unsupported_media_emits_error_event_without_calling_provider(
    tmp_path: Path, capability: str, payload: bytes, error_word: str
) -> None:
    (tmp_path / "f").write_bytes(payload)
    agent = _agent(tmp_path, **{capability: False})
    called = False

    class _Block:
        async def stream_turn(self, _request):
            nonlocal called
            called = True
            if False:
                yield  # pragma: no cover

    with patch("mycode.agent.get_provider_adapter", return_value=_Block()):
        events = [e async for e in agent.achat("x", attachments=["f"])]

    assert called is False
    assert [e.type for e in events] == ["error"]
    assert error_word in events[0].data["message"]


def test_run_forwards_attachments(tmp_path: Path) -> None:
    (tmp_path / "n.txt").write_text("sync hello", encoding="utf-8")
    cap = _Capture()
    with patch("mycode.agent.get_provider_adapter", return_value=cap):
        result = _agent(tmp_path).run("x", attachments=["n.txt"])
    assert result.error is None
    assert "sync hello" in cap.user_content[-1]["text"]
