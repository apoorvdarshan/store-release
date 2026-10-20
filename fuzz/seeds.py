#!/usr/bin/env python3
"""Write a starting corpus for one fuzz target from the repository's own files.

    python3 -I fuzz/seeds.py <target> <corpus dir>

Targets: config, release_notes, png, switches, redact. Seeds follow each target's
input layout (a leading mode byte), so the fuzzer starts from real configs, real
release-note headings and well-formed PNGs instead of empty input. Nothing is
committed: the corpus is rebuilt in each CI run.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

TARGETS = ("config", "release_notes", "png", "switches", "redact")
NOTES = (
    "ios-v1.2.3\n## ios-v1.2.3\nFixed a crash when saving.\n\n## android-v1.2.3\nThe same for Android.\n",
    "android-v3.0\n## android-v3.0\n- New widget\n- Faster sync\n## android-v3.0\nA duplicate section.\n",
    "v2.0\r\n## v2.0\r\nWindows line endings.\r\n## v1.0\r\nOlder notes.\r\n",
    "Unreleased\n## Unreleased\n- Not a release\n## v1.0\n\n",
)


def seeds(target: str) -> list[bytes]:
    if target == "config":
        sources = sorted(common.ROOT.glob("examples/*/store-release.toml")) + sorted(
            common.ROOT.glob("tests/canary/*.toml"))
        found = [b"\x00" + path.read_bytes() for path in sources]
        return found + [b"\x01" + bytes(range(64)), b"\x02" + b"../store/x.png"]
    if target == "release_notes":
        found = [b"\x00\x00" + text.encode() for text in NOTES]
        changelog = common.ROOT / "CHANGELOG.md"
        if changelog.is_file():
            text = changelog.read_text(encoding="utf-8")
            heading = next((line[3:] for line in text.splitlines() if line.startswith("## [0")), "Unreleased")
            found.append(b"\x00\x00" + f"{heading}\n{text}".encode())
        return found + [b"\x01" + bytes(range(48))]
    if target == "png":
        found = []
        for colour_type, depths in sorted(common.PNG_DEPTHS.items()):
            for bit_depth in depths:
                trns = {0: b"\x00\x00", 2: b"\x00" * 6, 3: b"\x00"}.get(colour_type)
                for with_trns in (False, True) if trns else (False,):
                    found.append(b"\x00" + common.build_png(
                        4, 3, colour_type, bit_depth, bytes(range(0, 256, 7)),
                        palette_entries=2 ** min(bit_depth, 8), trns=trns if with_trns else None,
                        extra=((b"sRGB", b"\x00"),), idat_pieces=2))
        return found + [b"\x01" + bytes(range(96))]
    return [bytes(range(n, n + 64)) for n in (0, 64, 128)] + [b"\x00" * 64, b"\xff" * 64]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    written = seeds(args.target)
    for seed in written:
        (args.out / hashlib.sha256(seed).hexdigest()[:16]).write_bytes(seed)
    print(f"{args.target}: {len(written)} seeds in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
