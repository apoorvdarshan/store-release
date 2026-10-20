#!/usr/bin/env python3
"""Fuzz the release notes reader (store_release.release_notes).

    python3 -I fuzz/fuzz_release_notes.py [libFuzzer flags] [corpus dir]

The GitHub Release body for a tag is its `## <tag>` section. Properties:

- section_for_tag and notes_for_tag raise nothing but ValueError;
- they agree with an independent line-by-line reading: one non-empty section is
  returned (stripped, as written), and two sections or an empty one fail rather
  than publishing whichever comes first;
- the pull request check (duplicate_headings) reports every duplicate that would
  fail a release, for tags git accepts, in files whose only line break is LF or CRLF.

Input: the first byte picks raw text (first line = tag) or a structured file built
from headings and bodies.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (puts the repository root on sys.path)

import atheris  # noqa: E402

with atheris.instrument_imports():
    from store_release import release_notes

release_notes.NOTES_RELPATH = "RELEASE_NOTES.md"

# Tags as an app's release tags look (ios-v7.1.2, android-v3.0, v1.0.0-beta.1).
REALISTIC_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Line breaks str.splitlines() honours besides LF (CRLF is normalised first).
OTHER_LINE_BREAKS = "\r\x0b\x0c\x1c\x1d\x1e\x85  "
PADDING = ("", " ", "  ", "\t", "\r", " \r", " ", "\x0b", " ")
FILLER = ("", "notes", "- fixed a crash", "## ", "##", "### sub", " ## not a heading", "```", "\r", "Unreleased")


def oracle_matches(tag: str, text: str) -> list[str]:
    """The stripped body of every `## ` section whose title is tag, reading LF-separated lines."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append((line[3:], []))
        elif sections:
            sections[-1][1].append(line)
    return ["\n".join(body).strip() for title, body in sections if title.strip() == tag]


def structured(fdp: atheris.FuzzedDataProvider) -> tuple[str, str]:
    tags = [fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 12)) for _ in range(fdp.ConsumeIntInRange(1, 3))]
    tags.append("ios-v1.2.3")
    lines: list[str] = []
    for _ in range(fdp.ConsumeIntInRange(0, 12)):
        kind = fdp.ConsumeIntInRange(0, 3)
        if kind == 0:
            pad_left, pad_right = fdp.PickValueInList(PADDING), fdp.PickValueInList(PADDING)
            lines.append(f"## {pad_left}{fdp.PickValueInList(tags)}{pad_right}")
        elif kind == 1:
            lines.append(fdp.PickValueInList(FILLER))
        else:
            lines.append(fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 24)))
    separator = fdp.PickValueInList(("\n", "\r\n", "\n\n", "\r"))
    return fdp.PickValueInList(tags), separator.join(lines)


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        tag, text = structured(fdp)
    else:
        raw = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
        tag, _, text = raw.partition("\n")

    matches = oracle_matches(tag, text)
    if tag == "Unreleased" or len(matches) > 1 or (matches and not matches[0]):
        expected_error = True
    else:
        expected_error = False
    try:
        notes = release_notes.section_for_tag(tag, text)
    except ValueError:
        if not expected_error:
            raise AssertionError(f"section_for_tag({tag!r}) failed; one usable section expected: {matches!r}")
    else:
        if expected_error:
            raise AssertionError(f"section_for_tag({tag!r}) returned {notes!r}; expected a failure for {matches!r}")
        assert notes == (matches[0] if matches else None), (notes, matches)
        if notes is not None:
            assert notes and notes == notes.strip() and notes in text, notes

    try:
        published = release_notes.notes_for_tag(tag, text)
    except ValueError:
        assert expected_error or not matches, (tag, matches)
    else:
        assert published == matches[0], (published, matches)

    duplicates = release_notes.duplicate_headings(text)
    assert len(duplicates) == len(set(duplicates)), duplicates
    normalised = text.replace("\r\n", "\n")
    if (len(matches) > 1 and REALISTIC_TAG.fullmatch(tag)
            and not any(ch in normalised for ch in OTHER_LINE_BREAKS)):
        assert tag in duplicates, f"{tag!r} has {len(matches)} sections but the PR check reports only {duplicates!r}"


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
