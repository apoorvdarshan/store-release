#!/usr/bin/env python3
"""Print the reviewed RELEASE_NOTES.md section for an exact release tag.

A `## <tag>` section (e.g. `## v1.0.1`) becomes the GitHub Release notes. When the
tag has no section, or RELEASE_NOTES.md does not exist, nothing is printed and the
release uses GitHub's generated notes instead. A section that exists but is empty,
or a tag with more than one section, is a mistake and fails.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NOTES = Path(__file__).resolve().parents[1] / "RELEASE_NOTES.md"


def section_for(text: str | None, tag: str) -> str | None:
    """The body under `## <tag>`, or None when the tag has no section.

    `text` is None when RELEASE_NOTES.md is missing, which counts as no notes."""
    if text is None:
        return None
    matches = []
    for section in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        title, _, body = section.partition("\n")
        if title.strip() == tag:
            matches.append(body.strip())
    if len(matches) > 1:
        raise ValueError(f"RELEASE_NOTES.md has {len(matches)} ## {tag} sections; keep exactly one")
    if matches and not matches[0]:
        raise ValueError(f"the ## {tag} section in RELEASE_NOTES.md is empty")
    return matches[0] if matches else None


def read_notes(path: Path | None = None) -> str | None:
    """RELEASE_NOTES.md's text, or None when the file does not exist."""
    try:
        return (path or NOTES).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--out", type=Path, help="write the notes here (an empty file means generated notes)")
    args = parser.parse_args(argv)
    text = read_notes()
    notes: str | None = None
    try:
        notes = section_for(text, args.tag)
    except ValueError as exc:
        parser.error(str(exc))  # exits
    if text is None:
        print("RELEASE_NOTES.md does not exist; using generated notes", file=sys.stderr)
    elif notes is None:
        print(f"RELEASE_NOTES.md has no ## {args.tag} section; using generated notes", file=sys.stderr)
    if args.out:
        args.out.write_text(notes + "\n" if notes else "", encoding="utf-8")
    elif notes:
        print(notes)


if __name__ == "__main__":
    main()
