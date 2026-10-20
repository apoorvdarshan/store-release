#!/usr/bin/env python3
"""Print reviewed release notes for an exact tag; fail rather than publish empty notes.

RELEASE_NOTES.md sections are headed `## <tag>` (e.g. `## ios-v7.1.2`), one per tag. The text
becomes the GitHub Release body. Store What's New comes from APPSTORE.md and
PLAYSTORE.md instead (scripts/store/prepare_whats_new.py).

    release_notes.py ios-v7.2               notes, or fail when there are none
    release_notes.py --if-present ios-v7.2  notes, or nothing when there are none
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def section_for_tag(tag: str, text: str | None = None) -> str | None:
    """The tag's notes, or None without a section (or file).

    An empty section fails, and so do duplicate sections: a copied or merged
    heading must never silently publish whichever section comes first.
    """
    if tag == 'Unreleased':
        raise ValueError('the Unreleased section is not a release; use the exact tag')
    if text is None:
        path = ROOT / 'RELEASE_NOTES.md'
        if not path.is_file():
            return None
        text = path.read_text(encoding='utf-8')
    matches = []
    for section in re.split(r'^## ', text, flags=re.MULTILINE)[1:]:
        title, _, body = section.partition('\n')
        if title.strip() == tag:
            matches.append(body.strip())
    if len(matches) > 1:
        raise ValueError(f'RELEASE_NOTES.md has {len(matches)} ## {tag} sections; keep exactly one')
    if matches and not matches[0]:
        raise ValueError('The release note section is empty')
    return matches[0] if matches else None


def notes_for_tag(tag: str, text: str | None = None) -> str:
    notes = section_for_tag(tag, text)
    if notes is None:
        raise ValueError(f'Add a reviewed ## {tag} section to RELEASE_NOTES.md before tagging')
    return notes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tag')
    parser.add_argument('--if-present', action='store_true', help='print nothing instead of failing without notes')
    args = parser.parse_args(argv)
    try:
        notes = section_for_tag(args.tag) if args.if_present else notes_for_tag(args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    if notes is None:
        print(f'no ## {args.tag} section in RELEASE_NOTES.md', file=sys.stderr)
    else:
        print(notes)


if __name__ == '__main__':
    main()
