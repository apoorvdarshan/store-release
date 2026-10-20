#!/usr/bin/env python3
"""Print store automation switch status. Never contacts a store.

Quit All ships on iOS only, so only the IOS_* switches exist. ios_release.py
`gates` is the authority that validates them; this is a readable summary that
uses the same rules, so a value the gates would reject shows as INVALID rather
than as off.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping

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

SWITCHES = [MASTER] + [name for name, _ in OPERATION_SWITCHES]
NUMBERS = (POLL_SECONDS, WAIT_MINUTES)


def describe_switch(env: Mapping[str, str], name: str) -> tuple[str, str]:
    """(state, text) for one switch; state is "on", "off" or "invalid"."""
    raw = env.get(name, "")
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return "on", "true"
    if value in FALSE_VALUES:
        return "off", "false"
    return "invalid", f"INVALID ({raw!r}; any release or validation run fails until it is true/false or unset)"


def describe_number(env: Mapping[str, str], spec: tuple[str, int, int, int]) -> tuple[str, str]:
    name, default, low, high = spec
    raw = env.get(name, "")
    try:
        value = parse_bounded_int(spec, raw)
    except ReleaseError:
        return "invalid", (
            f"INVALID ({raw!r}; must be a whole number from {low} to {high}; "
            "any release or validation run fails until it is fixed)"
        )
    return "ok", str(value) if raw.strip() else f"default ({default})"


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("ios",), default="ios")
    parser.parse_args(argv)
    env = os.environ if env is None else env

    print("Store automation switches (false = nothing reaches the store):")
    states: dict[str, str] = {}
    for name in SWITCHES:
        state, text = describe_switch(env, name)
        states[name] = state
        print(f"  {name}={text}")
    for spec in NUMBERS:
        state, text = describe_number(env, spec)
        states[spec[0]] = state
        print(f"  {spec[0]}={text}")

    invalid = [name for name, state in states.items() if state == "invalid"]
    if states[MASTER] != "on":
        print(f"  note: {MASTER} is not on, so every iOS store operation is skipped")
    if invalid:
        print(f"WARNING: invalid values: {', '.join(invalid)} — release and validation runs fail until they are fixed")
    if any(state == "on" for state in states.values()):
        print("WARNING: one or more store switches are on")
    elif not invalid:
        print("all switches off — no upload, listing change, review submission or release will run")


if __name__ == "__main__":
    main()
