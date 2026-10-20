#!/usr/bin/env python3
"""Print the reviewed RELEASE_NOTES.md section for an exact tag.

Prints nothing (exit 0) when there is no `## <tag>` section, or no
RELEASE_NOTES.md at all, so the release workflow lets GitHub generate the notes
instead. A section that exists but is empty is an error, and so is more than one
section for the same tag: reviewed notes are never published blank or picked
from an ambiguous file.
"""
import argparse
import re
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('tag')
parser.add_argument('--file', type=Path, default=Path(__file__).resolve().parents[1] / 'RELEASE_NOTES.md')
args = parser.parse_args()

text = args.file.read_text(encoding='utf-8') if args.file.is_file() else ''
sections = re.split(r'^## ', text, flags=re.MULTILINE)[1:]
matches = []
for section in sections:
    title, _, body = section.partition('\n')
    if title.strip() == args.tag:
        matches.append(body.strip())
if len(matches) > 1:
    parser.error(f'{args.file.name} has {len(matches)} "## {args.tag}" sections; keep exactly one')
if matches:
    if not matches[0]:
        parser.error(f'the ## {args.tag} section in {args.file.name} is empty')
    print(matches[0])
else:
    print(f'no ## {args.tag} section in {args.file.name}; GitHub will generate the release notes',
          file=sys.stderr)
