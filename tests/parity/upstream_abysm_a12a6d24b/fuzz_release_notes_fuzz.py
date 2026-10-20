#!/usr/bin/env python3
"""Fuzz scripts/release_notes.py, which picks a tag's GitHub Release notes.

For any RELEASE_NOTES.md text and tag, section() must return exactly that tag's
`## <tag>` body, None when the tag has no section (or is "Unreleased"), or raise
ValueError only when the tag has more than one section. It must never raise
anything else.

  python3 fuzz/release_notes_fuzz.py -max_total_time=60
"""
from __future__ import annotations

import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
with atheris.instrument_imports():
    import release_notes

TAGS = ["v1.0.0", "v1.0.1", "v1.0", "Unreleased", "unreleased", " v1.0.1", "v1.0.1 ", ""]
TOKENS = ["## ", "\n", "\r\n", "### ", "#", " ", "- ", "v1.0.0", "v1.0.1", "v1.0", "Unreleased",
          "notes", "```", "\t", " "]


def check(text: str, tag: str) -> None:
    case = f"text={text!r} tag={tag!r}"
    titles = [title for title, _ in release_notes.headings(text)]
    try:
        body = release_notes.section(text, tag)
    except ValueError:
        assert titles.count(tag) > 1, f"ValueError without a duplicate section: {case}"
        assert tag in release_notes.duplicate_headings(text), case
        return
    if body is None:
        assert tag.strip().lower() == "unreleased" or tag not in titles, f"missed a section: {case}"
        return
    assert tag.strip().lower() != "unreleased", f"Unreleased notes would be published: {case}"
    assert titles.count(tag) == 1, case
    assert (tag, body) in release_notes.headings(text), case
    assert body == body.strip(), case


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    tag = fdp.PickValueInList(TAGS) if fdp.ConsumeIntInRange(0, 3) else fdp.ConsumeUnicodeNoSurrogates(12)
    parts = []
    for _ in range(fdp.ConsumeIntInRange(0, 12)):
        choice = fdp.ConsumeIntInRange(0, 5)
        if choice <= 1:
            # A heading line, usually for this tag or another release.
            title = tag if fdp.ConsumeBool() else fdp.PickValueInList(TAGS)
            parts.append(fdp.PickValueInList(["## ", "##  ", "### ", "#"]) + title + fdp.PickValueInList(["\n", "\r\n", " \n", ""]))
        elif choice <= 3:
            parts.append(fdp.PickValueInList(TOKENS))
        else:
            parts.append(fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 8)))
    check("".join(parts), tag)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
