#!/usr/bin/env python3
"""Extract listing text fields from APPSTORE.md / PLAYSTORE.md into store metadata dirs."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PLAY_CAPS = {
    "title": 30,
    "short_description": 80,
    "full_description": 4000,
}
IOS_CAPS = {
    "name": 30,
    "subtitle": 30,
    "promotional_text": 170,
    "keywords": 100,
    "description": 4000,
}


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def first_fence_after(text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\s*$"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(pattern, line):
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    break
                if lines[j].strip().startswith("```"):
                    body: list[str] = []
                    for k in range(j + 1, len(lines)):
                        if lines[k].strip().startswith("```"):
                            return "\n".join(body).strip()
                        body.append(lines[k])
                    fail(f"unclosed fence under {heading}")
            fail(f"no fence under {heading}")
    fail(f"missing heading: {heading}")


def write_capped(path: Path, content: str, field: str, cap: int) -> None:
    text = content.strip()
    if len(text) > cap:
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        fail(f"{field} is {len(text)} chars (max {cap}): {shown}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "store" / "metadata")
    parser.add_argument(
        "--platform",
        choices=("play", "ios", "all"),
        default="all",
        help="Which store docs to parse (default: all)",
    )
    args = parser.parse_args()

    if args.platform in ("play", "all"):
        play = (ROOT / "PLAYSTORE.md").read_text(encoding="utf-8")
        write_capped(
            args.out / "play" / "en-US" / "title.txt",
            first_fence_after(play, "1. App Name"),
            "play title",
            PLAY_CAPS["title"],
        )
        write_capped(
            args.out / "play" / "en-US" / "short_description.txt",
            first_fence_after(play, "2. Short Description"),
            "play short_description",
            PLAY_CAPS["short_description"],
        )
        write_capped(
            args.out / "play" / "en-US" / "full_description.txt",
            first_fence_after(play, "3. Full Description"),
            "play full_description",
            PLAY_CAPS["full_description"],
        )

    if args.platform in ("ios", "all"):
        ios = (ROOT / "APPSTORE.md").read_text(encoding="utf-8")
        write_capped(
            args.out / "ios" / "en-US" / "name.txt",
            first_fence_after(ios, "App Name"),
            "ios name",
            IOS_CAPS["name"],
        )
        write_capped(
            args.out / "ios" / "en-US" / "subtitle.txt",
            first_fence_after(ios, "Subtitle (30 chars max)"),
            "ios subtitle",
            IOS_CAPS["subtitle"],
        )
        write_capped(
            args.out / "ios" / "en-US" / "promotional_text.txt",
            first_fence_after(ios, "Promotional Text (170 chars max)"),
            "ios promotional_text",
            IOS_CAPS["promotional_text"],
        )
        write_capped(
            args.out / "ios" / "en-US" / "keywords.txt",
            first_fence_after(ios, "Keywords (100 chars max)"),
            "ios keywords",
            IOS_CAPS["keywords"],
        )
        write_capped(
            args.out / "ios" / "en-US" / "description.txt",
            first_fence_after(ios, "Description"),
            "ios description",
            IOS_CAPS["description"],
        )
        for heading, filename in (
            ("Privacy URL", "privacy_url.txt"),
            ("Support URL", "support_url.txt"),
            ("Marketing URL", "marketing_url.txt"),
        ):
            write(
                args.out / "ios" / "en-US" / filename,
                first_fence_after(ios, heading),
            )

    print(f"wrote listing text under {args.out}")


if __name__ == "__main__":
    main()
