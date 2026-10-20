#!/usr/bin/env python3
"""Copy the configured screenshot folders' PNGs into a staging folder for ASC + Play upload.

App Store screenshots are staged as RGB PNGs: App Store Connect refuses files
that store an alpha channel, even when every pixel is opaque. Transparent pixels
are laid over black once, before any resize, and the colour profile is kept.
The committed screenshot files are only read, never rewritten. The CLI stages
into --out (under $RUNNER_TEMP), never into the app checkout.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from store_release import UNCONFIGURED
from store_release.ios import asc as asc_release  # PNG checks and the Apple Watch sizes, no App Store Connect calls

# App constants, set by store_release.context.configure() from store-release.toml.
ROOT: Path = UNCONFIGURED
# android.screenshots.dir (None without [android.screenshots]) and its staged folder.
PLAY_SOURCE: Path | None = UNCONFIGURED
PLAY_PHONE_DIR: Path = UNCONFIGURED
# The staging root. None means ROOT/store/metadata/screenshots, the upstream layout
# the module API and the ported tests use; the CLI always passes --out.
STAGING: Path | None = None
# The configured WATCH set's folder (repository-relative), or None without one.
WATCH_DIR: str | None = UNCONFIGURED


@dataclass(frozen=True)
class AscSet:
    """One App Store Connect screenshot set: its source folder, staged folder and sizes."""

    label: str
    source: Path
    dest: Path
    # Portrait sizes ASC accepts; anything else is fitted onto the first one.
    accepted: tuple[tuple[int, int], ...]


# Portrait sizes ASC accepts per display type; anything else is fitted onto the first.
ACCEPTED_SIZES = {
    "APP_IPHONE_67": ((1290, 2796), (1284, 2778)),
    "APP_IPAD_PRO_3GEN_129": ((2064, 2752), (2048, 2732)),
    # One set takes both screens: outer 1398x2034 and inner 2007x2853.
    "APP_IPHONE_DUO": ((1398, 2034), (2007, 2853)),
}
# Every set the App Store listing gets, in upload order (the configured non-WATCH
# ios.screenshots.sets). The staged folder names are the ones ios/asc.py reads.
ASC_SETS: tuple[AscSet, ...] = UNCONFIGURED


def staging_dir() -> Path:
    """Where staged files go: --out, else ROOT/store/metadata/screenshots (the module default)."""
    return STAGING if STAGING is not None else ROOT / "store" / "metadata" / "screenshots"


def watch_set() -> AscSet | None:
    """The configured Apple Watch set, under the current ROOT, or None without one.

    It is staged only when its folder exists. Every Watch size App Store
    Connect takes is accepted as it is (ios/asc.py picks each frame's display
    type by its size); another Watch-shaped frame is fitted onto the Series 10
    size.
    """
    if WATCH_DIR is None:
        return None
    return AscSet(
        "Apple Watch",
        ROOT / WATCH_DIR,
        staging_dir() / "ios" / asc_release.WATCH_FOLDER,
        tuple(asc_release.WATCH_DISPLAY_TYPES),
    )


def restage(out: Path) -> None:
    """Point STAGING and every staged folder at out (the CLI's --out)."""
    global STAGING, ASC_SETS, PLAY_PHONE_DIR
    STAGING = out
    ASC_SETS = tuple(AscSet(s.label, s.source, out / "ios" / s.dest.name, s.accepted) for s in ASC_SETS)
    PLAY_PHONE_DIR = out / "play" / "phone"


def shown(path: Path) -> Path:
    """A path relative to ROOT when it is inside it (as upstream printed it), else as it is."""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


# A frame that is not an accepted ASC size is fitted onto the set's first size only
# when its shape is this close, so a frame from another device's set fails staging.
ASC_ASPECT_TOLERANCE = 0.03
# PNG chunks that describe colour, copied from the source to the staged file
# (Pillow writes an ICC profile, iCCP, itself).
PNG_COLOUR_CHUNKS = (b"sRGB", b"gAMA", b"cHRM", b"cICP")

# Google Play phone screenshots (portrait ~9:16)
PLAY_MIN_SIDE = 320
PLAY_MAX_SIDE = 3840
PLAY_PORTRAIT_ASPECT = 9 / 16  # width / height
PLAY_ASPECT_TOLERANCE = 0.08
# Play Console phone screenshot cap (App Store allows 10).
PLAY_PHONE_MAX = 8


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def uploads_enabled() -> bool:
    return os.environ.get("STORE_UPLOAD_SCREENSHOTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def try_import_pillow():
    try:
        from PIL import Image  # type: ignore[import-untyped]

        return Image
    except ImportError:
        return None


def read_png_size(path: Path) -> tuple[int, int]:
    Image = try_import_pillow()
    if Image is not None:
        with Image.open(path) as img:
            return img.size
    # PNG IHDR without Pillow
    with path.open("rb") as f:
        header = f.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        fail(f"not a PNG: {path}")
    w = int.from_bytes(header[16:20], "big")
    h = int.from_bytes(header[20:24], "big")
    return w, h


def fit_and_pad_rgba(
    Image, src, target_w: int, target_h: int, fill=(0, 0, 0, 255)
):
    src_w, src_h = src.size
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (target_w, target_h), fill)
    ox = (target_w - new_w) // 2
    oy = (target_h - new_h) // 2
    canvas.paste(resized, (ox, oy), resized if resized.mode == "RGBA" else None)
    return canvas


def opaque_rgb(Image, img):
    """The image as RGB, with transparent pixels laid over black."""
    rgba = img.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, (0, 0, 0))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def colour_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """The source PNG's colour chunks other than its ICC profile (Pillow keeps that one)."""
    try:
        chunks = asc_release.png_chunks(raw, strict=False)
    except ValueError:
        return []
    return [(kind, data) for kind, data in chunks if kind in PNG_COLOUR_CHUNKS]


def prepare_asc_png(Image, path: Path, asc_set: AscSet) -> bytes:
    """An RGB PNG of the frame at an accepted size, without alpha, colour profile kept."""
    from PIL import PngImagePlugin  # type: ignore[import-untyped]

    target_w, target_h = asc_set.accepted[0]
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        icc_profile = img.info.get("icc_profile")
        # Flatten once, before any resize, so translucent pixels are blended a single time.
        out_img = opaque_rgb(Image, img)
    if out_img.size not in asc_set.accepted:
        out_img = fit_and_pad_rgba(Image, out_img, target_w, target_h).convert("RGB")
    info = PngImagePlugin.PngInfo()
    for kind, data in colour_chunks(raw):
        info.add(kind, data)
    out = io.BytesIO()
    options = {"icc_profile": icc_profile} if icc_profile else {}
    out_img.save(out, format="PNG", pnginfo=info, **options)
    return out.getvalue()


def stores_alpha(path: Path) -> bool:
    """True when a PNG stores alpha (colour type 4 or 6, or a tRNS chunk)."""
    try:
        chunks = asc_release.png_chunks(path.read_bytes(), strict=False)
    except ValueError:
        fail(f"not a PNG: {path}")
    return asc_release.png_stores_alpha(chunks)


def play_portrait_ok(width: int, height: int) -> bool:
    if width <= 0 or height <= 0 or height <= width:
        return False
    if min(width, height) < PLAY_MIN_SIDE or max(width, height) > PLAY_MAX_SIDE:
        return False
    aspect = width / height
    return abs(aspect - PLAY_PORTRAIT_ASPECT) <= PLAY_ASPECT_TOLERANCE


def validate_play_png(path: Path, *, width: int, height: int) -> None:
    if not play_portrait_ok(width, height):
        fail(
            f"{path.name}: Play phone screenshot must be portrait ~9:16 with each side "
            f"between {PLAY_MIN_SIDE} and {PLAY_MAX_SIDE}px (got {width}x{height})"
        )


def clear_destination_pngs(*dest_dirs: Path) -> None:
    for dest_dir in dest_dirs:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for stale in dest_dir.glob("*.png"):
            stale.unlink()


def validate_asc_png(path: Path, asc_set: AscSet, *, width: int, height: int) -> None:
    if (width, height) not in asc_set.accepted:
        accepted = ", ".join(f"{w}x{h}" for w, h in asc_set.accepted)
        fail(
            f"{path.name}: ASC {asc_set.label} screenshot must be {accepted} "
            f"(got {width}x{height}). Install Pillow (requirements-store.txt) "
            "so staging can resize automatically, or pre-export at the required size."
        )


def asc_frame_fits(asc_set: AscSet, width: int, height: int) -> bool:
    if (width, height) in asc_set.accepted:
        return True
    if width <= 0 or height <= 0:
        return False
    target_w, target_h = asc_set.accepted[0]
    return abs(width / height - target_w / target_h) <= ASC_ASPECT_TOLERANCE * target_w / target_h


def check_asc_frames(asc_set: AscSet, pngs: list[Path]) -> None:
    for path in pngs:
        w, h = read_png_size(path)
        if not asc_frame_fits(asc_set, w, h):
            accepted = ", ".join(f"{aw}x{ah}" for aw, ah in asc_set.accepted)
            fail(
                f"{asc_set.source.name}/{path.name}: {w}x{h} is not shaped like an ASC "
                f"{asc_set.label} screenshot ({accepted}); is it in the wrong folder?"
            )


def numbered_pngs(source: Path) -> list[Path]:
    if not source.is_dir():
        fail(f"screenshot source missing: {source.relative_to(ROOT)}")

    # Store uploads use numbered frames only (01-….png), in file-name order.
    pngs = sorted(
        p
        for p in source.glob("*.png")
        if len(p.name) >= 3 and p.name[0:2].isdigit() and p.name[2] == "-"
    )
    if not pngs:
        fail(
            f"no numbered PNG files (NN-name.png) under {source.relative_to(ROOT)}"
        )
    return pngs


def stage(*, dry_run: bool, platform: str = "all") -> list[Path]:
    staging_ios = platform in ("ios", "all")
    ios_sets = [(asc_set, numbered_pngs(asc_set.source)) for asc_set in ASC_SETS] if staging_ios else []
    # The Watch set is optional: without store/screenshots/watch/ nothing is
    # staged for it, and stale staged Watch frames are still removed below.
    watch = watch_set()
    if staging_ios and watch is not None and watch.source.is_dir():
        ios_sets.append((watch, numbered_pngs(watch.source)))
    staging_play = platform in ("play", "all") and PLAY_SOURCE is not None
    play_pngs = numbered_pngs(PLAY_SOURCE) if staging_play else []

    if len(play_pngs) > PLAY_PHONE_MAX:
        skipped = ", ".join(p.name for p in play_pngs[PLAY_PHONE_MAX:])
        print(f"note: Play phone cap is {PLAY_PHONE_MAX}; not staging: {skipped}")
        play_pngs = play_pngs[:PLAY_PHONE_MAX]

    for asc_set, pngs in ios_sets:
        check_asc_frames(asc_set, pngs)

    will_upload = uploads_enabled()
    Image = try_import_pillow()
    if will_upload and Image is None:
        for path in play_pngs:
            w, h = read_png_size(path)
            validate_play_png(path, width=w, height=h)
        for asc_set, pngs in ios_sets:
            for path in pngs:
                w, h = read_png_size(path)
                validate_asc_png(path, asc_set, width=w, height=h)
                # Without Pillow the file is copied as it is, so it must already be RGB.
                if stores_alpha(path):
                    fail(
                        f"{asc_set.source.name}/{path.name}: App Store screenshots cannot store an alpha "
                        "channel; install Pillow (requirements-store.txt) so staging can flatten it, "
                        "or re-export it as RGB"
                    )

    if not dry_run:
        ios_dests = [asc_set.dest for asc_set, _ in ios_sets]
        watch_dest = watch.dest if watch is not None else staging_dir() / "ios" / asc_release.WATCH_FOLDER
        if staging_ios and watch_dest not in ios_dests:
            # No Watch source: frames an earlier staging left must not be uploaded.
            ios_dests.append(watch_dest)
        clear_destination_pngs(*([PLAY_PHONE_DIR] if play_pngs else []), *ios_dests)

    copied: list[Path] = []
    for path in play_pngs:
        w, h = read_png_size(path)

        if dry_run:
            print(
                f"dry-run: would stage play/{path.name} ({w}x{h}) → "
                f"{shown(PLAY_PHONE_DIR)}"
            )
            continue

        play_dest = PLAY_PHONE_DIR / path.name
        if will_upload:
            if Image is not None and not play_portrait_ok(w, h):
                with Image.open(path) as img:
                    img = img.convert("RGBA")
                    target_h = min(PLAY_MAX_SIDE, max(h, PLAY_MIN_SIDE * 16 // 9))
                    target_w = int(round(target_h * PLAY_PORTRAIT_ASPECT))
                    target_w = min(PLAY_MAX_SIDE, max(PLAY_MIN_SIDE, target_w))
                    play_img = fit_and_pad_rgba(Image, img, target_w, target_h)
                # The padded frame itself must be a size Play takes.
                padded_w, padded_h = play_img.size
                if not play_portrait_ok(padded_w, padded_h):
                    fail(
                        f"{path.name}: padding {w}x{h} onto 9:16 gave {padded_w}x{padded_h}, which is "
                        f"not a Play phone screenshot size (portrait ~9:16, each side {PLAY_MIN_SIDE}-{PLAY_MAX_SIDE}px)"
                    )
                out = io.BytesIO()
                play_img.save(out, format="PNG")
                play_bytes = out.getvalue()
            else:
                validate_play_png(path, width=w, height=h)
                play_bytes = path.read_bytes()
        else:
            play_bytes = path.read_bytes()

        play_dest.write_bytes(play_bytes)
        copied.append(play_dest)

    for asc_set, pngs in ios_sets:
        for path in pngs:
            w, h = read_png_size(path)

            if dry_run:
                print(
                    f"dry-run: would stage {asc_set.source.name}/{path.name} ({w}x{h}) → "
                    f"{shown(asc_set.dest)}"
                )
                continue

            ios_dest = asc_set.dest / path.name
            if Image is not None:
                ios_bytes = prepare_asc_png(Image, path, asc_set)
            elif will_upload:
                validate_asc_png(path, asc_set, width=w, height=h)
                ios_bytes = path.read_bytes()
            else:
                ios_bytes = path.read_bytes()
            ios_dest.write_bytes(ios_bytes)
            copied.append(ios_dest)

    if not dry_run:
        ios_counts = "".join(
            f" + {len(pngs)} ASC {asc_set.label}" for asc_set, pngs in ios_sets
        )
        print(f"staged {len(play_pngs)} Play phone{ios_counts} screenshot(s)")
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned copies only (no filesystem changes)",
    )
    parser.add_argument(
        "--platform",
        choices=("play", "ios", "all"),
        default="all",
        help="Which store's screenshots to stage (default: all)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Staging folder (the CLI always passes one; the module default is "
        "store/metadata/screenshots under the app)",
    )
    args = parser.parse_args()
    if args.platform == "ios" and not ASC_SETS:
        fail("no [[ios.screenshots.sets]] in store-release.toml")
    if args.platform == "play" and PLAY_SOURCE is None:
        fail("no [android.screenshots] in store-release.toml")
    if args.out is not None:
        restage(args.out)
    stage(dry_run=args.dry_run, platform=args.platform)


if __name__ == "__main__":
    main()
