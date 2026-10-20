#!/usr/bin/env python3
"""Print reviewed release notes for an exact tag; fail rather than publish empty notes.

The release notes file (release_notes.path, default RELEASE_NOTES.md) has one
section per tag, headed `## <tag>` (e.g. `## ios-v7.1.2`). The text becomes the
GitHub Release body. Store What's New comes from the listing docs instead
(text/whats_new.py).

    release-notes ios-v7.2               notes, or fail when there are none
    release-notes --if-present ios-v7.2  notes, or nothing when there are none
    release-notes --check-headings       fail when a `## ` heading appears twice
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from store_release import UNCONFIGURED

ROOT: Path = UNCONFIGURED
NOTES_RELPATH: str = UNCONFIGURED


def section_for_tag(tag: str, text: str | None = None) -> str | None:
    """The tag's notes, or None without a section (or file).

    An empty section fails, and so do duplicate sections: a copied or merged
    heading must never silently publish whichever section comes first.
    """
    if tag == 'Unreleased':
        raise ValueError('the Unreleased section is not a release; use the exact tag')
    if text is None:
        path = ROOT / NOTES_RELPATH
        if not path.is_file():
            return None
        text = path.read_text(encoding='utf-8')
    matches = []
    for section in re.split(r'^## ', text, flags=re.MULTILINE)[1:]:
        title, _, body = section.partition('\n')
        if title.strip() == tag:
            matches.append(body.strip())
    if len(matches) > 1:
        raise ValueError(f'{NOTES_RELPATH} has {len(matches)} ## {tag} sections; keep exactly one')
    if matches and not matches[0]:
        raise ValueError('The release note section is empty')
    return matches[0] if matches else None


def notes_for_tag(tag: str, text: str | None = None) -> str:
    notes = section_for_tag(tag, text)
    if notes is None:
        raise ValueError(f'Add a reviewed ## {tag} section to {NOTES_RELPATH} before tagging')
    return notes


def duplicate_headings(text: str) -> list[str]:
    """`## ` headings that appear more than once, in first-seen order."""
    seen: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith('## '):
            title = line[3:].strip()
            seen[title] = seen.get(title, 0) + 1
    return [title for title, count in seen.items() if count > 1]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tag', nargs='?')
    parser.add_argument('--if-present', action='store_true', help='print nothing instead of failing without notes')
    parser.add_argument('--check-headings', action='store_true',
                        help='fail when any ## heading appears twice (pull request checks)')
    args = parser.parse_args(argv)
    if args.check_headings:
        if args.tag or args.if_present:
            parser.error('--check-headings takes no tag')
        path = ROOT / NOTES_RELPATH
        if not path.is_file():
            print(f'no {NOTES_RELPATH}; nothing to check')
            return
        duplicates = duplicate_headings(path.read_text(encoding='utf-8'))
        if duplicates:
            print(f"error: {NOTES_RELPATH} repeats these ## headings: {', '.join(duplicates)}; keep exactly one each",
                  file=sys.stderr)
            raise SystemExit(1)
        print(f'{NOTES_RELPATH}: every ## heading appears once')
        return
    if not args.tag:
        parser.error('the tag is required')
    try:
        notes = section_for_tag(args.tag) if args.if_present else notes_for_tag(args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    if notes is None:
        print(f'no ## {args.tag} section in {NOTES_RELPATH}', file=sys.stderr)
    else:
        print(notes)


if __name__ == '__main__':
    main()
