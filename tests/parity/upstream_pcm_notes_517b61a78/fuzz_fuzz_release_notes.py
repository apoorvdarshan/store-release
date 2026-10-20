#!/usr/bin/env python3
"""Fuzz target: the release-notes lookup in scripts/release_notes.py.

The iOS Release workflow publishes the `## <tag>` section of RELEASE_NOTES.md as the
GitHub Release notes. For any notes text and tag, `section_for` must return the body
of the one section titled with the tag, None when there is no such section, and
raise ValueError for an empty or repeated section, and nothing else.

  python3 fuzz/fuzz_release_notes.py -max_total_time=60   # needs Atheris
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts")]

try:
    import atheris
except ImportError:  # test_fuzz_targets.py replays the target without Atheris
    atheris = None

with atheris.instrument_imports() if atheris else contextlib.nullcontext():
    from release_notes import section_for

from fuzz_input import FuzzInput

TAGS = ("v1.0.0", "v1.0.1", "v2.0", "")


def TestOneInput(data: bytes) -> None:
    fuzz = FuzzInput(data)
    tag = fuzz.pick(TAGS) if fuzz.flag() else fuzz.text(4, exclude="\n")
    assert section_for(None, tag) is None

    if fuzz.flag():
        # Arbitrary text, with headings wherever the bytes put them.
        text = fuzz.text(32)
        try:
            notes = section_for(text, tag)
        except ValueError as exc:
            assert tag in str(exc), exc
            return
        assert notes is None or (notes and notes == notes.strip() and notes in text), notes
        return

    # Notes built from known sections: no `#` outside the headings added here, so
    # the expected result follows from the sections themselves.
    sections = []
    for _ in range(fuzz.int_in(0, 5)):
        if fuzz.flag():
            title = fuzz.pick((tag, f" {tag} ", f"{tag}x", f"x{tag}", f"{tag}\t"))
        else:
            title = fuzz.text(3, exclude="#\n")
        sections.append((title, fuzz.text(8, exclude="#")))
    text = fuzz.text(6, exclude="#") + "".join(f"\n## {title}\n{body}" for title, body in sections)
    bodies = [body.strip() for title, body in sections if title.strip() == tag]

    try:
        notes = section_for(text, tag)
    except ValueError:
        assert len(bodies) > 1 or bodies == [""], (text, tag)
        return
    if bodies:
        assert len(bodies) == 1 and bodies[0] and notes == bodies[0], (text, tag, notes)
    else:
        assert notes is None, (text, tag, notes)


def main() -> None:
    if atheris is None:
        raise SystemExit("Atheris is required: pip install --require-hashes -r fuzz/requirements.txt")
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
