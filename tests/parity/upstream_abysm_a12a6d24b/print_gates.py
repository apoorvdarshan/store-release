#!/usr/bin/env python3
"""Print the iOS release switches (repository Variables). Never touches a store.

What a run may actually do is resolved by `ios_release.py gates`, which rejects
invalid values; this summary shows them as INVALID instead of hiding them as off.
"""
from __future__ import annotations

import os
import re

GATES = (
    "IOS_RELEASE_AUTOMATION",
    "IOS_CREATE_VERSION",
    "IOS_UPLOAD_BUILD",
    "IOS_UPLOAD_LISTING",
    "IOS_UPLOAD_WHATS_NEW",
    "IOS_UPLOAD_SCREENSHOTS",
    "IOS_UPLOAD_CREATIVE_ASSETS",
    "IOS_SUBMIT_REVIEW",
    "IOS_PUBLIC_RELEASE",
)
# (name, default, lowest, highest): the same bounds as ios_release.py.
TIMING = (("IOS_BUILD_POLL_SECONDS", 30, 10, 600), ("IOS_BUILD_WAIT_MINUTES", 120, 10, 300))
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}


def describe(name: str) -> tuple[str, bool]:
    """How a switch reads, and whether it is on."""
    raw = os.environ.get(name, "")
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return "true", True
    if value in FALSE_VALUES:
        return ("false" if value else "false (unset)"), False
    return f"INVALID {raw!r} (a release run fails)", False


def describe_timing(name: str, default: int, low: int, high: int) -> str:
    raw = os.environ.get(name, "")
    value = raw.strip()
    if not value:
        return f"{default} (default)"
    if re.fullmatch(r"[0-9]+", value) and low <= int(value) <= high:
        return value
    return f"INVALID {raw!r} (must be {low}-{high}; a release run fails)"


def main() -> None:
    print("iOS release switches (false = nothing reaches App Store Connect):")
    switches = {name: describe(name) for name in GATES}
    invalid = [name for name, (shown, _) in switches.items() if shown.startswith("INVALID")]
    for name, (shown, _) in switches.items():
        print(f"  {name}={shown}")
    for name, default, low, high in TIMING:
        shown = describe_timing(name, default, low, high)
        if shown.startswith("INVALID"):
            invalid.append(name)
        print(f"  {name}={shown}")
    if invalid:
        print(f"ERROR: {', '.join(invalid)} invalid: every release run fails until fixed")
    if not switches["IOS_RELEASE_AUTOMATION"][1]:
        print("note: IOS_RELEASE_AUTOMATION is off, so every iOS store operation is skipped")
    elif not any(switches[name][1] for name in GATES[1:]):
        print("note: the master switch is on but no store operation is enabled")
    else:
        print("WARNING: one or more live App Store Connect operations are on")


if __name__ == "__main__":
    main()
