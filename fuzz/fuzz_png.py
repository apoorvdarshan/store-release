#!/usr/bin/env python3
"""Fuzz the PNG integrity and alpha checks and the App Store screenshot staging.

    python3 -I fuzz/fuzz_png.py [libFuzzer flags] [corpus dir]

App Store Connect refuses screenshots and product page images that store an alpha
channel, and a truncated or corrupt file must fail before anything is uploaded.
Properties:

- png_chunks (strict and lenient), png_size, png_stores_alpha, jpeg_size and the
  staging colour-chunk reader raise nothing but ValueError on any bytes;
- what the strict reader accepts re-serialises byte for byte (every CRC checked),
  starts with IHDR, ends with IEND, and the lenient reader returns the same chunks;
- every well-formed PNG (each colour type and bit depth, tRNS, split IDAT, extra
  chunks) is accepted with its size, and stores alpha exactly when its colour type
  has alpha or it has a tRNS chunk;
- any truncation and any single changed byte of a well-formed PNG is rejected;
- staging (prepare_asc_png, with Pillow) turns every well-formed PNG, alpha or not,
  into a PNG that passes the strict check, stores no alpha and has an accepted size.

Input: the first byte picks raw bytes or a PNG built from the remaining input.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402  (puts the repository root on sys.path)

import atheris  # noqa: E402

with atheris.instrument_imports(include=["store_release"]):
    from store_release import screenshots
    from store_release.ios import asc

Image = screenshots.try_import_pillow()
# A tiny set keeps staging fast; the real sets differ only in their sizes.
SMALL_SET = screenshots.AscSet("fuzz", Path("source"), Path("staged"), ((24, 52), (20, 44)))
WORK = Path(tempfile.mkdtemp(prefix="fuzz-png-"))


def check_any_bytes(raw: bytes) -> None:
    try:
        strict = asc.png_chunks(raw)
    except ValueError:
        strict = None
    try:
        lenient = asc.png_chunks(raw, strict=False)
    except ValueError:
        lenient = None
    for chunks in (strict, lenient):
        if chunks is not None:
            assert chunks[0][0] == b"IHDR" and len(chunks[0][1]) >= 13, chunks[0]
            width, height = asc.png_size(chunks)
            assert (width, height) == (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
            assert asc.png_stores_alpha(chunks) == (raw[25] in (4, 6) or any(k == b"tRNS" for k, _ in chunks))
    if strict is not None:
        assert lenient == strict, "the lenient reader disagrees with an accepted file"
        assert strict[-1][0] == b"IEND", strict[-1]
        serialised = common.png_bytes(strict)
        assert raw[:len(serialised)] == serialised, "accepted chunks do not re-serialise to the file"
    for kind, _ in screenshots.colour_chunks(raw):
        assert kind in screenshots.PNG_COLOUR_CHUNKS, kind
    size = asc.jpeg_size(raw)
    assert size is None or all(0 <= side <= 0xFFFF for side in size), size


def ancillary(fdp: atheris.FuzzedDataProvider) -> tuple[bytes, bytes]:
    """A valid ancillary chunk that may precede PLTE and IDAT (zzZz is an unknown one)."""
    kind = common.pick(fdp, (b"tEXt", b"gAMA", b"sRGB", b"pHYs", b"cICP", b"zzZz"))
    if kind == b"tEXt":
        data = b"Comment\x00" + fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 16)).replace(b"\x00", b"")
    elif kind == b"gAMA":
        data = fdp.ConsumeIntInRange(1, 2**31 - 1).to_bytes(4, "big")
    elif kind == b"sRGB":
        data = bytes([fdp.ConsumeIntInRange(0, 3)])
    elif kind == b"pHYs":
        data = fdp.ConsumeBytes(8).ljust(8, b"\x00") + bytes([fdp.ConsumeIntInRange(0, 1)])
    elif kind == b"cICP":
        data = bytes([fdp.ConsumeIntInRange(1, 12), fdp.ConsumeIntInRange(1, 18), 0, fdp.ConsumeIntInRange(0, 1)])
    else:
        data = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 12))
    return kind, data


def built_png(fdp: atheris.FuzzedDataProvider) -> tuple[bytes, int, int, bool]:
    """A well-formed PNG: every colour type and bit depth, palette indices in range, tRNS within spec."""
    width = fdp.ConsumeIntInRange(1, 40)
    height = fdp.ConsumeIntInRange(1, 40)
    colour_type = common.pick(fdp, sorted(common.PNG_DEPTHS))
    bit_depth = common.pick(fdp, common.PNG_DEPTHS[colour_type])
    palette = 2 ** min(bit_depth, 8)
    trns = None
    if colour_type in (0, 2, 3) and fdp.ConsumeBool():
        length = {0: 2, 2: 6, 3: fdp.ConsumeIntInRange(1, min(palette, 8))}[colour_type]
        trns = fdp.ConsumeBytes(length).ljust(length, b"\x00")
        if colour_type in (0, 2) and bit_depth < 16:
            # Each sample of the transparent colour must fit the bit depth.
            limit = 2 ** bit_depth
            samples = [int.from_bytes(trns[i:i + 2], "big") % limit for i in range(0, length, 2)]
            trns = b"".join(sample.to_bytes(2, "big") for sample in samples)
    extra = tuple(ancillary(fdp) for _ in range(fdp.ConsumeIntInRange(0, 2)))
    raw = common.build_png(width, height, colour_type, bit_depth, fdp.ConsumeBytes(fdp.ConsumeIntInRange(1, 64)),
                           palette_entries=palette, trns=trns, extra=extra,
                           idat_pieces=fdp.ConsumeIntInRange(1, 4), level=fdp.ConsumeIntInRange(0, 9))
    return raw, width, height, colour_type in (4, 6) or trns is not None


def stage(raw: bytes, has_alpha: bool) -> None:
    """The staged copy of a well-formed PNG passes the strict check without alpha."""
    if Image is None:
        return
    source = WORK / "frame.png"
    source.write_bytes(raw)
    staged = screenshots.prepare_asc_png(Image, source, SMALL_SET)
    chunks = asc.png_chunks(staged)
    assert not asc.png_stores_alpha(chunks), f"staged copy stores alpha (source alpha: {has_alpha})"
    assert asc.png_size(chunks) in SMALL_SET.accepted, asc.png_size(chunks)


def check_built(fdp: atheris.FuzzedDataProvider) -> None:
    raw, width, height, has_alpha = built_png(fdp)
    chunks = asc.png_chunks(raw)
    assert asc.png_size(chunks) == (width, height)
    assert asc.png_stores_alpha(chunks) == has_alpha
    check_any_bytes(raw)

    cut = fdp.ConsumeIntInRange(0, len(raw) - 1)
    try:
        asc.png_chunks(raw[:cut])
    except ValueError:
        pass
    else:
        raise AssertionError(f"a PNG truncated to {cut} of {len(raw)} bytes passed the strict check")

    position = fdp.ConsumeIntInRange(0, len(raw) - 1)
    corrupt = bytearray(raw)
    corrupt[position] ^= fdp.ConsumeIntInRange(1, 255)
    try:
        asc.png_chunks(bytes(corrupt))
    except ValueError:
        pass
    else:
        raise AssertionError(f"a PNG with byte {position} of {len(raw)} changed passed the strict check")
    check_any_bytes(bytes(corrupt))

    if fdp.ConsumeIntInRange(0, 7) == 0:
        stage(raw, has_alpha)


def TestOneInput(data: bytes) -> None:
    if not data:
        return
    if data[0] & 1:
        check_built(atheris.FuzzedDataProvider(data[1:]))
    else:
        check_any_bytes(data[1:])


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
