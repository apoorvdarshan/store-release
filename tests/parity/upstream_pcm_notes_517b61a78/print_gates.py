#!/usr/bin/env python3
"""Print the iOS store automation switches. Never mutates a store.

Each value is shown as ios_release.py reads it: true, false (missing counts as
false) or INVALID. An invalid value fails the release run even when the master
switch is off; ios_release.py `gates` is what enforces that.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ios_release import (  # noqa: E402
    FALSE_VALUES,
    MASTER,
    OPERATION_SWITCHES,
    POLL_SECONDS,
    TRUE_VALUES,
    WAIT_MINUTES,
    ReleaseError,
    parse_bounded_int,
)

GATES = (MASTER,) + tuple(name for name, _ in OPERATION_SWITCHES)
TIMING = (POLL_SECONDS, WAIT_MINUTES)


def describe(name: str) -> tuple[str, bool]:
    """How a switch reads, and whether it is on."""
    raw = os.environ.get(name, "")
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return "true", True
    if value in FALSE_VALUES:
        return ("false" if value else "false (unset)"), False
    return f"INVALID {raw!r} (a release run fails)", False


def describe_timing(spec: tuple[str, int, int, int]) -> str:
    name, default = spec[0], spec[1]
    raw = os.environ.get(name, "")
    try:
        value = parse_bounded_int(spec, raw)
    except ReleaseError as exc:
        return f"INVALID {raw!r} ({exc}; a release run fails)"
    return str(value) if raw.strip() else f"{default} (default)"


def main() -> None:
    print("iOS store automation switches (missing = false = off):")
    shown = {name: describe(name)[0] for name in GATES}
    shown.update({spec[0]: describe_timing(spec) for spec in TIMING})
    for name, text in shown.items():
        print(f"  {name}={text}")
    invalid = [name for name, text in shown.items() if text.startswith("INVALID")]
    if invalid:
        print(f"WARNING: these values are invalid, so every release run fails: {', '.join(invalid)}")
        return
    if not describe(MASTER)[1]:
        print(f"  note: {MASTER} is off, so every iOS store operation is skipped")
        return
    on = [name for name in GATES[1:] if describe(name)[1]]
    if on:
        print(f"WARNING: live App Store operations are on: {', '.join(on)}")
    else:
        print("master switch on, but every store operation is off")


if __name__ == "__main__":
    main()
