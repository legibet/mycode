"""Attachment input for user messages.

Conversion rules in one place so the SDK, CLI, and HTTP server all produce the
same content blocks for the same input.
"""

from __future__ import annotations

import builtins
import html
from base64 import b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path
from typing import Self

from mycode.messages import ContentBlock, document_block, image_block, text_block
from mycode.utils import resolve_path

SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
SUPPORTED_DOCUMENT_MIME_TYPES = frozenset({"application/pdf"})


@dataclass(frozen=True, kw_only=True)
class Attachment:
    """File path, raw bytes, or text snippet attached to a user message.

    Construct via ``Attachment.path`` / ``.bytes`` / ``.text``; ``kw_only`` stops a
    bare ``Attachment("notes.txt")`` from silently becoming text instead of a path.
    """

    source: Path | builtins.bytes | str
    media_type: str | None = None
    name: str | None = None

    @classmethod
    def path(cls, path: str | Path, *, name: str | None = None) -> Self:
        """``name`` defaults to the file's basename when omitted."""

        return cls(source=Path(path), name=name)

    @classmethod
    def bytes(cls, data: builtins.bytes, *, media_type: str, name: str | None = None) -> Self:
        """``media_type`` must be a supported image type or ``application/pdf``."""

        if not media_type:
            raise ValueError("Attachment.bytes requires a non-empty media_type")
        return cls(source=bytes(data), media_type=media_type, name=name)

    @classmethod
    def text(cls, data: str, *, name: str) -> Self:
        """Wrapped as a named ``<file>`` snippet in the user message."""

        if not name:
            raise ValueError("Attachment.text requires a non-empty name")
        return cls(source=data, name=name)


AttachmentLike = str | Path | Attachment


def build_attachment_blocks(
    attachments: Sequence[AttachmentLike],
    *,
    cwd: str,
) -> list[ContentBlock]:
    """Return one content block per attachment, in input order.

    ``str`` / ``Path`` items are treated as ``Attachment.path``. Raises
    ``ValueError`` on a missing path, a directory, a binary file that is
    neither image nor PDF, undecodable text, or an unsupported ``media_type``.
    """

    blocks: list[ContentBlock] = []
    for item in attachments:
        att = item if isinstance(item, Attachment) else Attachment.path(item)
        match att.source:
            case bytes() as data:
                media_type = att.media_type or ""
                if media_type in SUPPORTED_IMAGE_MIME_TYPES:
                    blocks.append(image_block(b64encode(data).decode("ascii"), mime_type=media_type, name=att.name))
                elif media_type in SUPPORTED_DOCUMENT_MIME_TYPES:
                    blocks.append(document_block(b64encode(data).decode("ascii"), mime_type=media_type, name=att.name))
                else:
                    supported = sorted(SUPPORTED_IMAGE_MIME_TYPES | SUPPORTED_DOCUMENT_MIME_TYPES)
                    raise ValueError(f"unsupported media_type {media_type!r}; want one of {supported}")
            case Path() as raw:
                path = Path(resolve_path(str(raw), cwd=cwd))
                if not path.exists():
                    raise ValueError(f"attachment not found: {raw}")
                if path.is_dir():
                    raise ValueError(f"attachment is a directory: {raw}")
                display = att.name or path.name
                if mime := detect_image_mime_type(path):
                    blocks.append(
                        image_block(b64encode(path.read_bytes()).decode("ascii"), mime_type=mime, name=display)
                    )
                elif mime := detect_document_mime_type(path):
                    blocks.append(
                        document_block(b64encode(path.read_bytes()).decode("ascii"), mime_type=mime, name=display)
                    )
                else:
                    try:
                        text = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError(f"unsupported attachment {raw}: not image, PDF, or UTF-8 text") from exc
                    blocks.append(_wrap_file(text, display))
            case str() as text:
                blocks.append(_wrap_file(text, att.name or ""))
    return blocks


def unsupported_attachment_block(
    *,
    name: str,
    mime_type: str,
    kind: str,
    path: str | None = None,
) -> ContentBlock:
    """Text-block stand-in for an image/PDF block a model can't ingest.

    Pass ``path`` (e.g. the original ``@path`` typed by the user) to keep it on
    ``meta.path`` for UI labels; omit it when replaying history where the
    on-disk path is no longer authoritative.
    """

    label = "image input" if kind == "image" else "PDF input"
    safe_name = html.escape(name, quote=True)
    safe_mime = html.escape(mime_type, quote=True)
    body = f"Current model does not support {label}."
    meta: dict[str, object] = {"attachment": True}
    if path is not None:
        meta["path"] = path
    return text_block(
        f'<file name="{safe_name}" media_type="{safe_mime}" kind="{kind}">{body}</file>',
        meta=meta,
    )


def detect_image_mime_type(path: Path) -> str | None:
    """Sniff PNG/JPEG/GIF/WebP magic bytes; fall back to the filename extension."""

    try:
        with path.open("rb") as file:
            header = file.read(16)
    except OSError:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = guess_type(path.name)
    return guessed if guessed in SUPPORTED_IMAGE_MIME_TYPES else None


def detect_document_mime_type(path: Path) -> str | None:
    """Return ``application/pdf`` if the file is a PDF (by magic bytes or extension)."""

    try:
        with path.open("rb") as file:
            if file.read(5).startswith(b"%PDF-"):
                return "application/pdf"
    except OSError:
        pass
    guessed, _ = guess_type(path.name)
    return "application/pdf" if guessed == "application/pdf" else None


def _wrap_file(text: str, name: str) -> ContentBlock:
    safe = html.escape(name, quote=True)
    return text_block(f'<file name="{safe}">\n{text}\n</file>', meta={"attachment": True, "path": name})
