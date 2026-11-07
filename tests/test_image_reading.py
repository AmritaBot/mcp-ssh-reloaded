"""Tests for the experimental read_image tool and image validation."""

from __future__ import annotations

import asyncio
import base64
import struct
import zlib
from typing import Any, cast

import pytest
from fastmcp.utilities.types import Image

from mcp_ssh_reloaded import server
from mcp_ssh_reloaded.image_validation import ImageValidationError, sniff_image
from mcp_ssh_reloaded.models import (
    BinaryFileContent,
    ErrorCategory,
    ImagePayload,
    SSHError,
)
from mcp_ssh_reloaded.services import SSHService


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)
    )


def make_png(width: int = 1, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def make_jpeg(width: int = 2, height: int = 3) -> bytes:
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sos = b"\x01\x01\x00\x00\x3f\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe0"
        + struct.pack(">H", len(app0) + 2)
        + app0
        + b"\xff\xc0"
        + struct.pack(">H", len(sof0) + 2)
        + sof0
        + b"\xff\xda"
        + struct.pack(">H", len(sos) + 2)
        + sos
        + b"\x00\x01\x02"
        + b"\xff\xd9"
    )


def make_gif(width: int = 4, height: int = 5) -> bytes:
    descriptor = b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
    image_data = b"\x02" + b"\x02\x4c\x01" + b"\x00"
    return (
        b"GIF89a"
        + struct.pack("<HH", width, height)
        + b"\x00\x00\x00"
        + descriptor
        + image_data
        + b"\x3b"
    )


def make_webp_vp8l(width: int = 7, height: int = 9) -> bytes:
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    payload = b"\x2f" + struct.pack("<I", bits)
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload + b"\x00"
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def sniff(data: bytes, max_pixels: int = 16_000_000):
    return sniff_image(data, max_pixels=max_pixels)


@pytest.mark.parametrize(
    ("payload", "mime", "width", "height"),
    [
        (make_png(3, 4), "image/png", 3, 4),
        (make_jpeg(5, 6), "image/jpeg", 5, 6),
        (make_gif(7, 8), "image/gif", 7, 8),
        (make_webp_vp8l(9, 10), "image/webp", 9, 10),
    ],
)
def test_sniff_supported_formats(payload, mime, width, height):
    info = sniff(payload)
    assert info.mime_type == mime
    assert (info.width, info.height) == (width, height)


def test_truncated_png_is_rejected():
    with pytest.raises(ImageValidationError):
        sniff(make_png()[:-12])


def test_png_without_iend_is_rejected():
    data = make_png()[:-12]
    with pytest.raises(ImageValidationError):
        sniff(data)


def test_broken_png_crc_is_rejected():
    data = bytearray(make_png())
    data[30] ^= 0xFF
    with pytest.raises(ImageValidationError):
        sniff(bytes(data))


def test_jpeg_without_eoi_is_rejected():
    with pytest.raises(ImageValidationError):
        sniff(make_jpeg()[:-2])


def test_gif_without_trailer_is_rejected():
    with pytest.raises(ImageValidationError):
        sniff(make_gif()[:-1])


def test_gif_without_image_data_is_rejected():
    header_only = b"GIF89a" + struct.pack("<HH", 4, 5) + b"\x00\x00\x00" + b"\x3b"
    with pytest.raises(ImageValidationError):
        sniff(header_only)


def test_png_without_idat_is_rejected():
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IEND", b"")
    with pytest.raises(ImageValidationError):
        sniff(data)


def test_text_is_rejected():
    with pytest.raises(ImageValidationError):
        sniff(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")


def test_png_header_with_garbage_body_is_rejected():
    with pytest.raises(ImageValidationError):
        sniff(b"\x89PNG\r\n\x1a\n" + b"garbage")


def test_oversized_pixels_are_rejected():
    with pytest.raises(ImageValidationError):
        sniff(make_png(5000, 5000), max_pixels=16_000_000)


def test_oversized_dimension_is_rejected():
    with pytest.raises(ImageValidationError):
        sniff(make_png(9000, 1))


class _FakeEngine:
    def __init__(self, content: BinaryFileContent):
        self._content = content

    async def read_binary(self, **kwargs) -> BinaryFileContent:
        return self._content


def _service_with(content: BinaryFileContent) -> SSHService:
    svc = SSHService()
    svc._engine = cast(Any, _FakeEngine(content))
    return svc


def test_service_returns_payload_for_valid_image():
    svc = _service_with(
        BinaryFileContent(path="/tmp/a.png", data=make_png(2, 2), size=10)
    )
    payload = asyncio.run(svc.read_image(server._conn("host"), "/tmp/a.png"))
    assert isinstance(payload, ImagePayload)
    assert payload.mime_type == "image/png"
    assert (payload.width, payload.height) == (2, 2)


def test_service_rejects_unsupported_payload():
    svc = _service_with(BinaryFileContent(path="/tmp/a.txt", data=b"hello", size=5))
    with pytest.raises(SSHError) as exc:
        asyncio.run(svc.read_image(server._conn("host"), "/tmp/a.txt"))
    assert "Unsupported image format" in exc.value.message


def test_service_propagates_read_errors():
    svc = _service_with(BinaryFileContent(path="/tmp/a.png", error="not found"))
    with pytest.raises(SSHError):
        asyncio.run(svc.read_image(server._conn("host"), "/tmp/a.png"))


class _FakeServerService:
    def __init__(
        self, payload: ImagePayload | None = None, error: Exception | None = None
    ):
        self._payload = payload
        self._error = error

    async def read_image(self, conn, path, **kwargs):
        if self._error:
            raise self._error
        return self._payload


def test_tool_returns_image_content(monkeypatch):
    payload = ImagePayload(
        path="/tmp/a.png", data=make_png(), mime_type="image/png", width=1, height=1
    )
    monkeypatch.setattr(server, "_svc", _FakeServerService(payload=payload))
    result = asyncio.run(server.read_image("host", "/tmp/a.png"))
    assert isinstance(result, Image)
    content = result.to_image_content()
    assert content.mimeType == "image/png"
    assert base64.b64decode(content.data) == payload.data


def test_tool_returns_error_text_on_rejection(monkeypatch):
    monkeypatch.setattr(
        server,
        "_svc",
        _FakeServerService(
            error=SSHError(ErrorCategory.PROTOCOL, "Unsupported image format")
        ),
    )
    result = asyncio.run(server.read_image("host", "/tmp/a.txt"))
    assert isinstance(result, str)
    assert "Unsupported image format" in result
