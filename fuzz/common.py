"""Shared helpers for the Atheris fuzz targets (fuzz/fuzz_*.py).

Importing this puts the repository root on sys.path, so `python3 -I fuzz/fuzz_<name>.py`
finds the store_release package the way run.py does. It never imports store_release
itself: each target imports it inside atheris.instrument_imports(), so the package is
instrumented for coverage.
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Valid bit depths for each PNG colour type (PNG spec, IHDR).
PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def png_chunk(kind: bytes, data: bytes) -> bytes:
    """One PNG chunk: length, type, data and the CRC over type and data."""
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def png_bytes(chunks: list[tuple[bytes, bytes]]) -> bytes:
    return PNG_SIGNATURE + b"".join(png_chunk(kind, data) for kind, data in chunks)


def png_row_bytes(width: int, colour_type: int, bit_depth: int) -> int:
    """Bytes per scanline, without the filter byte."""
    return (width * PNG_CHANNELS[colour_type] * bit_depth + 7) // 8


def build_png(width: int, height: int, colour_type: int, bit_depth: int, pixels: bytes, *,
              palette_entries: int = 1, trns: bytes | None = None,
              extra: tuple[tuple[bytes, bytes], ...] = (), idat_pieces: int = 1, level: int = 6) -> bytes:
    """A well-formed, non-interlaced PNG; pixels are cycled to fill every row (filter type 0)."""
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([bit_depth, colour_type, 0, 0, 0])
    chunks: list[tuple[bytes, bytes]] = [(b"IHDR", header)]
    chunks.extend(extra)
    if colour_type == 3:
        chunks.append((b"PLTE", bytes(3 * max(1, min(256, palette_entries)))))
    if trns is not None:
        chunks.append((b"tRNS", trns))
    row = png_row_bytes(width, colour_type, bit_depth)
    source = pixels or b"\x00"
    tiled = source * (row // len(source) + 2)
    raw = bytearray()
    for y in range(height):
        offset = y % len(source)
        raw.append(0)
        raw.extend(tiled[offset:offset + row])
    stream = zlib.compress(bytes(raw), level)
    pieces = max(1, min(idat_pieces, len(stream)))
    step = -(-len(stream) // pieces)
    for start in range(0, len(stream), step):
        chunks.append((b"IDAT", stream[start:start + step]))
    chunks.append((b"IEND", b""))
    return png_bytes(chunks)
