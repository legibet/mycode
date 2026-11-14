"""Files attached to a user message: what the model gets for each.

Text is included as its first ``read`` window. An image or a PDF is included
when the current model accepts it and it is within the limits. Any other file
is named by its path, and the model inspects it with its tools.
"""

from __future__ import annotations

import logging
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from typing import Literal

from pypdf import PdfReader

from mycode.attachments import (
    Attachment,
    build_attachment_blocks,
    detect_document_mime_type,
    detect_image_mime_type,
)
from mycode.messages import ContentBlock, document_block, image_block
from mycode_cli.tools import MAX_MEDIA_BYTES, fit_image, read_text_window

MAX_PDF_PAGES = 20

# pypdf logs a warning for every flaw it finds in a file.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def file_block(path: Path, *, name: str, supports_image: bool, supports_pdf: bool) -> ContentBlock:
    """Return the block that attaches the file at ``path``; ``name`` is what the model sees it called."""

    if detect_image_mime_type(path):
        kind = "image"
    elif detect_document_mime_type(path):
        kind = "document"
    else:
        try:
            with path.open(encoding="utf-8") as file:
                window = read_text_window(file)
        except UnicodeDecodeError:
            return _path_reference(path, name, "the file is not text, an image or a PDF")
        return _file_text(window.render(path), name)
    with path.open("rb") as file:
        data = file.read(MAX_MEDIA_BYTES + 1)
    return media_block(data, kind, path=path, name=name, supports_image=supports_image, supports_pdf=supports_pdf)


def media_block(
    data: bytes,
    kind: Literal["image", "document"],
    *,
    path: Path,
    name: str,
    supports_image: bool,
    supports_pdf: bool,
) -> ContentBlock:
    """Return the block for an image or PDF: its content, or a text block naming ``path`` when it is not included.

    ``path`` is where the file is, or where the caller saves an upload that is
    not included.
    """

    try:
        if kind == "image":
            if not supports_image:
                raise ValueError("the current model does not accept image input")
            data, mime_type = fit_image(data)
            return image_block(b64encode(data).decode("ascii"), mime_type=mime_type, name=Path(name).name)
        if not supports_pdf:
            raise ValueError("the current model does not accept PDF input")
        _check_pdf(data)
        return document_block(b64encode(data).decode("ascii"), mime_type="application/pdf", name=Path(name).name)
    except ValueError as reason:
        return _path_reference(path, name, str(reason))


def _check_pdf(data: bytes) -> None:
    """Raise ``ValueError`` with the reason when the PDF is over the limits or cannot be parsed."""

    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError(f"the file is larger than {MAX_MEDIA_BYTES // 2**20} MB")
    try:
        pages = len(PdfReader(BytesIO(data)).pages)
    except Exception as exc:
        # pypdf raises errors of many types on a damaged or encrypted file.
        raise ValueError("the PDF could not be parsed") from exc
    if pages > MAX_PDF_PAGES:
        raise ValueError(f"the PDF has {pages} pages and the limit is {MAX_PDF_PAGES}")


def _path_reference(path: Path, name: str, reason: str) -> ContentBlock:
    return _file_text(f"[Content not included: {reason}. The file is at {path}.]", name)


def _file_text(text: str, name: str) -> ContentBlock:
    return build_attachment_blocks([Attachment.text(text, name=name)])[0]
