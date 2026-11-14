"""Tests for the files the CLI attaches to a user message."""

from __future__ import annotations

from base64 import b64decode
from io import BytesIO
from pathlib import Path

import pytest
from conftest import image_bytes, pdf_bytes
from PIL import Image

from mycode.messages import ContentBlock
from mycode_cli.attachments import MAX_PDF_PAGES, file_block
from mycode_cli.tools import MAX_IMAGE_SIDE


def _attach(path: Path, data: bytes, *, supports_image: bool = True, supports_pdf: bool = True) -> ContentBlock:
    path.write_bytes(data)
    return file_block(path, name=str(path), supports_image=supports_image, supports_pdf=supports_pdf)


def _decoded(block: ContentBlock) -> Image.Image:
    assert block["type"] == "image"
    return Image.open(BytesIO(b64decode(block["data"])))


def test_image_within_the_limits_is_included_unchanged(tmp_path: Path) -> None:
    data = image_bytes((MAX_IMAGE_SIDE, 10))

    block = _attach(tmp_path / "shot.png", data)

    assert block["type"] == "image"
    assert (b64decode(block["data"]), block["mime_type"], block["name"]) == (data, "image/png", "shot.png")


def test_larger_image_is_scaled_down_to_jpeg(tmp_path: Path) -> None:
    # Transparent pixels must come out white, not black.
    block = _attach(tmp_path / "shot.png", image_bytes((MAX_IMAGE_SIDE * 2, MAX_IMAGE_SIDE), mode="RGBA"))

    assert block["type"] == "image"
    assert block["mime_type"] == "image/jpeg"
    image = _decoded(block)
    assert image.size == (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE // 2)
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_scaled_photo_keeps_its_exif_orientation(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored on its side: viewers rotate it by 90 degrees
    data = image_bytes((MAX_IMAGE_SIDE * 2, MAX_IMAGE_SIDE), "JPEG", exif=exif)

    image = _decoded(_attach(tmp_path / "photo.jpg", data))

    assert image.size == (MAX_IMAGE_SIDE // 2, MAX_IMAGE_SIDE)


def test_image_over_the_byte_limit_is_reencoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = image_bytes((100, 100), compress_level=0)
    monkeypatch.setattr("mycode_cli.tools.MAX_IMAGE_BYTES", len(data) - 1)

    block = _attach(tmp_path / "shot.png", data)

    assert block["type"] == "image"
    assert block["mime_type"] == "image/jpeg"
    assert _decoded(block).size == (100, 100)


def test_image_that_stays_over_the_byte_limit_is_named_by_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mycode_cli.tools.MAX_IMAGE_BYTES", 1)

    block = _attach(tmp_path / "shot.png", image_bytes((10, 10)))

    assert block["type"] == "text"
    assert "still too large after scaling down" in block["text"]


def test_pdf_within_the_page_limit_is_included(tmp_path: Path) -> None:
    data = pdf_bytes(MAX_PDF_PAGES)

    block = _attach(tmp_path / "report.pdf", data)

    assert block["type"] == "document"
    assert (b64decode(block["data"]), block["mime_type"], block["name"]) == (data, "application/pdf", "report.pdf")


@pytest.mark.parametrize(
    ("filename", "data", "supports", "reason"),
    [
        ("shot.png", image_bytes((10, 10)), {"supports_image": False}, "does not accept image input"),
        ("shot.png", image_bytes((100, 100), compress_level=0)[:1000], {}, "could not be decoded"),
        ("report.pdf", pdf_bytes(1), {"supports_pdf": False}, "does not accept PDF input"),
        ("report.pdf", pdf_bytes(MAX_PDF_PAGES + 1), {}, f"has {MAX_PDF_PAGES + 1} pages"),
        ("report.pdf", b"%PDF-1.7\nnot a pdf", {}, "could not be parsed"),
        ("blob.bin", b"\x00\x01\x02\xff\xfe", {}, "not text, an image or a PDF"),
    ],
)
def test_file_that_is_not_included_is_named_by_its_path(
    tmp_path: Path, filename: str, data: bytes, supports: dict[str, bool], reason: str
) -> None:
    path = tmp_path / filename

    block = _attach(path, data, **supports)

    assert block["type"] == "text"
    assert block.get("meta") == {"attachment": True, "path": str(path)}
    assert reason in block["text"]
    assert f"The file is at {path}." in block["text"]
