"""Sniff and validate image payloads before they reach an MCP client.

Acceptance is decided by magic bytes and container structure only; a file
extension or a caller-supplied MIME type is never trusted. A payload that is
not a complete, parseable PNG, JPEG, GIF or WebP is rejected as unsupported.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

DEFAULT_MAX_DIMENSION = 8192

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_MAX_CHUNKS = 4096


class ImageValidationError(ValueError):
    """Payload is not a supported, structurally valid image."""


@dataclass(frozen=True)
class ImageInfo:
    """Format and pixel dimensions recovered from an image payload."""

    mime_type: str
    width: int
    height: int


def sniff_image(
    data: bytes,
    *,
    max_pixels: int,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> ImageInfo:
    """Return format and dimensions, or raise ImageValidationError.

    The signature, the container structure and the declared size must agree.
    Truncated or malformed payloads are rejected rather than handed to a client.
    """
    if not data:
        raise ImageValidationError("empty payload")

    info = _parse_container(data)
    if info.width <= 0 or info.height <= 0:
        raise ImageValidationError("non-positive dimensions")
    if info.width > max_dimension or info.height > max_dimension:
        raise ImageValidationError("dimension limit exceeded")
    if info.width * info.height > max_pixels:
        raise ImageValidationError("pixel limit exceeded")
    return info


def _parse_container(data: bytes) -> ImageInfo:
    if data.startswith(_PNG_SIGNATURE):
        return _parse_png(data)
    if data.startswith(b"\xff\xd8\xff"):
        return _parse_jpeg(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _parse_gif(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _parse_webp(data)
    raise ImageValidationError("unsupported signature")


def _parse_png(data: bytes) -> ImageInfo:
    pos = len(_PNG_SIGNATURE)
    width = 0
    height = 0
    seen_ihdr = False

    for _ in range(_MAX_CHUNKS):
        if pos + 12 > len(data):
            raise ImageValidationError("truncated PNG chunk header")

        length = int.from_bytes(data[pos : pos + 4], "big")
        chunk_type = data[pos + 4 : pos + 8]
        chunk_end = pos + 8 + length + 4
        if chunk_end > len(data):
            raise ImageValidationError("truncated PNG chunk body")

        if not seen_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise ImageValidationError("PNG does not start with IHDR")
            width, height = struct.unpack(">II", data[pos + 8 : pos + 16])
            expected_crc = int.from_bytes(data[pos + 8 + length : chunk_end], "big")
            actual_crc = zlib.crc32(data[pos + 4 : pos + 8 + length]) & 0xFFFFFFFF
            if expected_crc != actual_crc:
                raise ImageValidationError("PNG IHDR checksum mismatch")
            seen_ihdr = True
        elif chunk_type == b"IEND":
            return ImageInfo("image/png", width, height)

        pos = chunk_end

    raise ImageValidationError("PNG has no IEND chunk")


def _parse_jpeg(data: bytes) -> ImageInfo:
    if len(data) < 4:
        raise ImageValidationError("truncated JPEG")
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise ImageValidationError("JPEG has no end-of-image marker")

    pos = 2
    width = 0
    height = 0

    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            raise ImageValidationError("JPEG segment marker expected")

        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xD9:
            break
        if pos + 4 > len(data):
            raise ImageValidationError("truncated JPEG segment header")

        segment_length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if segment_length < 2 or pos + 2 + segment_length > len(data):
            raise ImageValidationError("invalid JPEG segment length")

        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8:
                raise ImageValidationError("truncated JPEG frame header")
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")

        if marker == 0xDA:
            break
        pos += 2 + segment_length

    if width == 0 or height == 0:
        raise ImageValidationError("JPEG frame header not found")
    return ImageInfo("image/jpeg", width, height)


def _parse_gif(data: bytes) -> ImageInfo:
    if len(data) < 14:
        raise ImageValidationError("truncated GIF header")
    if not data.rstrip(b"\x00").endswith(b"\x3b"):
        raise ImageValidationError("GIF has no trailer")

    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return ImageInfo("image/gif", width, height)


def _parse_webp(data: bytes) -> ImageInfo:
    if len(data) < 20:
        raise ImageValidationError("truncated WebP header")

    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size + 8 > len(data):
        raise ImageValidationError("WebP RIFF size exceeds payload")

    chunk_type = data[12:16]

    if chunk_type == b"VP8X":
        if len(data) < 30:
            raise ImageValidationError("truncated WebP VP8X header")
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return ImageInfo("image/webp", width, height)

    if chunk_type == b"VP8 ":
        if len(data) < 30:
            raise ImageValidationError("truncated WebP VP8 frame")
        if data[23:26] != b"\x9d\x01\x2a":
            raise ImageValidationError("WebP VP8 start code missing")
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return ImageInfo("image/webp", width, height)

    if chunk_type == b"VP8L":
        if len(data) < 25:
            raise ImageValidationError("truncated WebP VP8L frame")
        if data[20] != 0x2F:
            raise ImageValidationError("WebP VP8L signature missing")
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return ImageInfo("image/webp", width, height)

    raise ImageValidationError("unsupported WebP chunk type")
