#!/usr/bin/env python3
"""Print the reviewed RELEASE_NOTES.md section for an exact release tag.

Exit status: 0 = section printed; 3 = no section for this tag, or no
RELEASE_NOTES.md at all (the release workflow then lets GitHub generate notes);
1 = the section exists but is empty, or the tag has more than one section.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NOTES = Path(__file__).resolve().parents[1] / "RELEASE_NOTES.md"
MISSING = 3


def headings(text: str) -> list[tuple[str, str]]:
    """Every `## <title>` section as (title, body)."""
    sections = []
    for chunk in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        title, _, body = chunk.partition("\n")
        sections.append((title.strip(), body.strip()))
    return sections


def duplicate_headings(text: str) -> list[str]:
    """Titles used by more than one `## ` section."""
    titles = [title for title, _ in headings(text)]
    return sorted({title for title in titles if titles.count(title) > 1})


def section(text: str, tag: str) -> str | None:
    """Body of the `## <tag>` section, or None when there is none.

    Raises ValueError when the tag has more than one section: which one is
    meant cannot be guessed.
    """
    if tag.strip().lower() == "unreleased":
        return None
    bodies = [body for title, body in headings(text) if title == tag]
    if len(bodies) > 1:
        raise ValueError(f"RELEASE_NOTES.md has {len(bodies)} '## {tag}' sections; keep exactly one")
    return bodies[0] if bodies else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="release tag, e.g. v1.0.1")
    args = parser.parse_args(argv)
    text = NOTES.read_text(encoding="utf-8") if NOTES.is_file() else ""
    try:
        body = section(text, args.tag)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if body is None:
        print(f"note: RELEASE_NOTES.md has no '## {args.tag}' section", file=sys.stderr)
        return MISSING
    if not body:
        print(f"error: the '## {args.tag}' section in RELEASE_NOTES.md is empty", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
