"""Stdlib PNG and JPEG writers for tests (no Pillow): every colour type, extra chunks, broken files."""
from __future__ import annotations

import functools
import zlib
from pathlib import Path

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


@functools.lru_cache(maxsize=64)
def png(width: int, height: int, colour_type: int = 2, *, chunks: tuple[tuple[bytes, bytes], ...] = (),
        value: int = 0) -> bytes:
    """A well-formed PNG of uniform pixels (8 bits per sample), with optional extra chunks before IDAT."""
    channels = CHANNELS[colour_type]
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, colour_type, 0, 0, 0])
    palette = chunk(b"PLTE", b"\x00\x00\x00") if colour_type == 3 else b""
    extra = b"".join(chunk(kind, data) for kind, data in chunks)
    row = b"\x00" + bytes([value]) * (width * channels)
    pixels = zlib.compress(row * height, 6)
    return SIGNATURE + chunk(b"IHDR", header) + palette + extra + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def rgb(width: int, height: int) -> bytes:
    return png(width, height, 2)


def rgba(width: int, height: int) -> bytes:
    return png(width, height, 6, value=255)


def with_trns(width: int, height: int) -> bytes:
    return png(width, height, 2, chunks=((b"tRNS", b"\x00" * 6),))


def with_iccp(width: int, height: int) -> bytes:
    profile = zlib.compress(b"not a real profile")
    return png(width, height, 2, chunks=((b"iCCP", b"test\x00\x00" + profile),))


def broken_crc(raw: bytes) -> bytes:
    data = bytearray(raw)
    idat = raw.index(b"IDAT")
    data[idat + 4] ^= 0xFF
    return bytes(data)


def truncated(raw: bytes) -> bytes:
    return raw[:-12]


def jpeg(width: int, height: int) -> bytes:
    """A JPEG header with a baseline start-of-frame segment (enough to read its size)."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x01\x11\x00" * 3
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def write(path: Path, raw: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def size(raw: bytes) -> tuple[int, int]:
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
