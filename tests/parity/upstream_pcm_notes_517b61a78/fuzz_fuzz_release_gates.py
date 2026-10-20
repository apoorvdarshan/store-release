#!/usr/bin/env python3
"""Fuzz target: the iOS release switches in scripts/store/ios_release.py.

`resolve_gates` turns the IOS_* repository variables, the triggering event and the
tag into what a release run may do. For any input it must either raise
ReleaseError or return a plan that keeps the safety rules in store/README.md:

- any invalid switch or timing value fails, even with the master switch off;
- only a `push` of the tag v<MARKETING_VERSION> releases; a manual run only
  validates and never uploads, submits or publishes;
- with the master switch off nothing is uploaded or published;
- review submission needs this run's build upload, and public release needs
  review submission.

  python3 fuzz/fuzz_release_gates.py -max_total_time=60   # needs Atheris
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts" / "store")]

try:
    import atheris
except ImportError:  # test_fuzz_targets.py replays the target without Atheris
    atheris = None

with atheris.instrument_imports() if atheris else contextlib.nullcontext():
    from ios_release import (
        FALSE_VALUES,
        MASTER,
        OPERATION_SWITCHES,
        POLL_SECONDS,
        TAG_PREFIX,
        TRUE_VALUES,
        VERSION_PATTERN,
        WAIT_MINUTES,
        ReleaseError,
        resolve_gates,
    )

from fuzz_input import FuzzInput

VALID_SWITCHES = ("true", "false", "TRUE", " yes ", "1", "0", "on", "off", "", "no", "\u00a0true")
INVALID_SWITCHES = ("maybe", "2", "tru e", "enabled", "\u0661")
VALID_NUMBERS = ("", "10", "30", "600", "120", "300", " 45 ")
INVALID_NUMBERS = ("9", "601", "301", "0", "-5", "1e3", "\u0663\u0660", "30.0", "x")
SWITCHES = (MASTER, *(name for name, _ in OPERATION_SWITCHES))


def TestOneInput(data: bytes) -> None:
    fuzz = FuzzInput(data)
    env: dict[str, str] = {}
    for name in SWITCHES:
        if fuzz.flag():
            env[name] = fuzz.choose(VALID_SWITCHES, INVALID_SWITCHES) if fuzz.int_in(0, 15) else fuzz.text(3)
    for name, *_ in (POLL_SECONDS, WAIT_MINUTES):
        if fuzz.flag():
            env[name] = fuzz.choose(VALID_NUMBERS, INVALID_NUMBERS) if fuzz.int_in(0, 15) else fuzz.text(3)
    event = fuzz.choose(("push", "workflow_dispatch"), ("pull_request", "schedule", "", "Push"))
    version = fuzz.choose(("1.0", "1.0.1", "2.3.4"), ("", "1.0 ", "v1.0")) if fuzz.int_in(0, 15) else fuzz.text(3)
    ref_name = fuzz.choose((TAG_PREFIX + version,), ("v1.0.1", "1.0", "main", "")) if fuzz.int_in(0, 15) else fuzz.text(3)
    override = fuzz.choose(("", "", "1.1", "1.0.2"), ("1", "1.2.3.4", " 1.1 ", "1.x")) if fuzz.int_in(0, 15) else fuzz.text(3)

    def value(name: str) -> str:
        return (env.get(name) or "").strip()

    def on(name: str) -> bool:
        return value(name).lower() in TRUE_VALUES

    def number_ok(spec: tuple[str, int, int, int]) -> bool:
        raw = value(spec[0])
        return not raw or (raw.isascii() and raw.isdigit() and spec[2] <= int(raw) <= spec[3])

    must_fail = (
        event not in {"push", "workflow_dispatch"}
        or any(value(name).lower() not in TRUE_VALUES | FALSE_VALUES for name in SWITCHES)
        or not number_ok(POLL_SECONDS)
        or not number_ok(WAIT_MINUTES)
        or (bool(override.strip()) and (event != "workflow_dispatch" or not VERSION_PATTERN.fullmatch(override.strip())))
        or (event == "push" and ref_name != TAG_PREFIX + version)
        or (event == "push" and on(MASTER) and on("IOS_SUBMIT_REVIEW") and not on("IOS_UPLOAD_BUILD"))
        or (event == "push" and on(MASTER) and on("IOS_PUBLIC_RELEASE") and not on("IOS_SUBMIT_REVIEW"))
    )
    try:
        plan = resolve_gates(env, event=event, ref_name=ref_name, marketing_version=version, validation_version=override)
    except ReleaseError:
        assert must_fail, ("rejected a valid configuration", env, event, ref_name, version, override)
        return
    assert not must_fail, ("accepted an invalid configuration", env, event, ref_name, version, override)

    if event == "workflow_dispatch":
        assert plan["mode"] == "validate" and not plan["master"], plan
        assert not any(plan[key] for _, key in OPERATION_SWITCHES), ("a manual run would publish", plan)
        assert plan["build"] and not plan["asc"], plan
    else:
        assert plan["mode"] == "release" and plan["master"] == on(MASTER), plan
        for name, key in OPERATION_SWITCHES:
            assert plan[key] == (on(MASTER) and on(name)), (name, plan)
        assert plan["build"] == plan["upload_build"], plan
    assert not plan["submit"] or plan["upload_build"], plan
    assert not plan["public_release"] or plan["submit"], plan
    assert POLL_SECONDS[2] <= plan["poll_seconds"] <= POLL_SECONDS[3], plan
    assert WAIT_MINUTES[2] <= plan["wait_minutes"] <= WAIT_MINUTES[3], plan


def main() -> None:
    if atheris is None:
        raise SystemExit("Atheris is required: pip install --require-hashes -r fuzz/requirements.txt")
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
