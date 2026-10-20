#!/usr/bin/env python3
"""Prepare Play whatsnew-* files and iOS what's-new text from listing docs."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def extract_fenced_after_heading(text: str, heading_prefix: str) -> str:
    """Return the first ``` fenced block after a markdown heading that starts with prefix."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading_prefix in line:
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    fail(f"no fenced block before next heading after {heading_prefix!r}")
                if lines[j].strip().startswith("```"):
                    body: list[str] = []
                    for k in range(j + 1, len(lines)):
                        if lines[k].startswith("## "):
                            fail(f"unclosed fence before next heading after {heading_prefix!r}")
                        if lines[k].strip().startswith("```"):
                            return "\n".join(body).strip()
                        body.append(lines[k])
                    fail(f"unclosed fence after heading matching {heading_prefix!r}")
            fail(f"no fenced block after heading matching {heading_prefix!r}")
    fail(f"heading matching {heading_prefix!r} not found")


# Both stores show What's New as plain text, so Markdown would appear literally.
MARKDOWN_SYNTAX = (
    (re.compile(r"\[[^\]\n]+\]\([^)\s]+\)"), "links like [text](url)"),
    (re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"), "bold like **text**"),
    (re.compile(r"`[^`\n]+`"), "code like `text`"),
    (re.compile(r"^ {0,3}#{1,6}\s", re.MULTILINE), "headings like # Title"),
)


def check_plain_text(label: str, text: str) -> None:
    """Fail when store text contains Markdown (bullets such as • or - are fine)."""
    found = [description for pattern, description in MARKDOWN_SYNTAX if pattern.search(text)]
    if found:
        fail(
            f"{label} must be plain text; the store would show Markdown literally "
            f"(found {', '.join(found)})"
        )


# Google Play's limit for a release's "What's new", per language.
PLAY_WHATS_NEW_MAX = 500


def write_play_whatsnew(block: str, out_dir: Path) -> list[Path]:
    """Split <locale>...</locale> blocks into whatsnew-LOCALE files.

    The upload action sends each file's bytes verbatim, so a file holds exactly
    the checked text: no trailing newline that would make 500 characters 501.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for match in re.finditer(
        r"<([A-Za-z]{2}(?:-[A-Za-z0-9]+)?)>\s*(.*?)\s*</\1>",
        block,
        flags=re.DOTALL,
    ):
        locale, body = match.group(1), match.group(2).strip()
        if not body:
            fail(f"empty what's new for {locale}")
        if len(body) > PLAY_WHATS_NEW_MAX:
            fail(f"what's new for {locale} exceeds {PLAY_WHATS_NEW_MAX} chars ({len(body)})")
        check_plain_text(f"what's new for {locale} in PLAYSTORE.md", body)
        path = out_dir / f"whatsnew-{locale}"
        path.write_text(body, encoding="utf-8")
        written.append(path)
    if not written:
        fail("no <locale> what's-new blocks found in PLAYSTORE.md")
    return written


# App Store Connect's limit for a version's "What's New in This Version".
IOS_WHATS_NEW_MAX = 4000


def check_ios_whats_new(block: str) -> str:
    """The iOS What's New text, or fail when App Store Connect would refuse it."""
    text = block.strip()
    if not text:
        fail("empty what's new in APPSTORE.md")
    if len(text) > IOS_WHATS_NEW_MAX:
        fail(f"what's new in APPSTORE.md exceeds {IOS_WHATS_NEW_MAX} chars ({len(text)})")
    check_plain_text("what's new in APPSTORE.md", text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        required=True,
        help="Release tag, e.g. android-v6.1 or ios-v6.1 (used for logging only)",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory")
    parser.add_argument(
        "--platform",
        choices=("play", "ios", "all"),
        default="all",
        help="Which store docs to parse (default: all)",
    )
    args = parser.parse_args()

    if args.platform in ("play", "all"):
        play = (ROOT / "PLAYSTORE.md").read_text(encoding="utf-8")
        play_block = extract_fenced_after_heading(play, "What's New")
        play_dir = args.out / "play"
        play_files = write_play_whatsnew(play_block, play_dir)
        print(f"  play locales: {len(play_files)} → {play_dir}")

    if args.platform in ("ios", "all"):
        ios = (ROOT / "APPSTORE.md").read_text(encoding="utf-8")
        ios_text = check_ios_whats_new(extract_fenced_after_heading(ios, "What's New"))
        ios_path = args.out / "ios" / "en-US" / "whats_new.txt"
        ios_path.parent.mkdir(parents=True, exist_ok=True)
        ios_path.write_text(ios_text + "\n", encoding="utf-8")
        print(f"  ios: {ios_path}")

    print(f"prepared what's new for {args.tag}")


if __name__ == "__main__":
    main()
