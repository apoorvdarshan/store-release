#!/usr/bin/env python3
"""Fuzz the iOS release switches in scripts/store/ios_release.py.

resolve_gates() decides what a release run may publish. For any repository
variables, event, tag and version it must either raise ReleaseError or return
a plan that keeps every release-safety rule:

- every switch value it accepted is a recognized true/false value, and every
  number it accepted is plain ASCII digits within its bounds;
- only tag pushes and manual runs are accepted;
- a manual run never uploads, lists, submits or releases anything;
- a tag run needs the tag to be exactly v<MARKETING_VERSION>;
- with the master switch off, nothing is published;
- review submission needs this run's upload; public release needs submission.

  python3 fuzz/release_gates_fuzz.py -max_total_time=60
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "store"))
with atheris.instrument_imports():
    import ios_release

SWITCHES = [ios_release.MASTER] + [name for name, _ in ios_release.OPERATION_SWITCHES]
NUMBERS = [ios_release.POLL_SECONDS, ios_release.WAIT_MINUTES]
OPERATIONS = [key for _, key in ios_release.OPERATION_SWITCHES]
VALID_SWITCHES = sorted(ios_release.TRUE_VALUES | ios_release.FALSE_VALUES) + ["TRUE", " Yes ", "Off\n"]
ODD_SWITCHES = ["enabled", "2", "-1", "tru", "true!", "\u0131", "on\x00", "\u00a0true"]
VALID_NUMBERS = ["", "10", "30", "45", " 120 ", "300", "600"]
ODD_NUMBERS = ["0", "9", "301", "601", "+30", "3e1", "0x1e", "1_000", "\u0663\u0660", "-10", "15.0", "\uff13\uff10"]
VALID_EVENTS = ["push", "workflow_dispatch"]
ODD_EVENTS = ["pull_request", "schedule", "release", "Push", ""]
VALID_VERSIONS = ["1.0", "1.0.1", "1.1", "2.0.0", "10.20.30"]
ODD_VERSIONS = ["1", "1.0.1.2", "01.0", "1..0", "1.0 ", "v1.0", "", "1.0-beta", "\u0661.0"]


def switch_on(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ios_release.TRUE_VALUES


def check(env: Mapping[str, str], event: str, ref_name: str, marketing_version: str,
          validation_version: str) -> None:
    try:
        gates = ios_release.resolve_gates(
            env, event=event, ref_name=ref_name, marketing_version=marketing_version,
            validation_version=validation_version)
    except ios_release.ReleaseError:
        return
    case = f"env={dict(env)!r} event={event!r} ref={ref_name!r} version={marketing_version!r} " \
           f"validation={validation_version!r} gates={gates!r}"

    for name in SWITCHES:
        value = (env.get(name) or "").strip().lower()
        assert value in ios_release.TRUE_VALUES | ios_release.FALSE_VALUES, f"accepted {name}: {case}"
    for name, default, low, high in NUMBERS:
        raw = (env.get(name) or "").strip()
        assert raw == "" or (raw.isascii() and raw.isdigit() and low <= int(raw) <= high), \
            f"accepted {name}: {case}"
    assert low_high(gates["poll_seconds"], ios_release.POLL_SECONDS), case
    assert low_high(gates["wait_minutes"], ios_release.WAIT_MINUTES), case

    assert event in {"push", "workflow_dispatch"}, case
    if validation_version.strip():
        assert event == "workflow_dispatch", case
        assert ios_release.VERSION_PATTERN.fullmatch(validation_version.strip()), case

    if event == "workflow_dispatch":
        assert gates["mode"] == "validate" and not gates["master"], case
        assert not any(gates[key] for key in OPERATIONS), f"a manual run may publish: {case}"
    else:
        assert gates["mode"] == "release", case
        assert ref_name == ios_release.TAG_PREFIX + marketing_version, f"wrong tag accepted: {case}"
        if not switch_on(env.get(ios_release.MASTER)):
            assert not gates["master"] and not any(gates[key] for key in OPERATIONS), \
                f"master off but something is on: {case}"

    assert not gates["submit"] or gates["upload_build"], case
    assert not gates["public_release"] or gates["submit"], case
    assert not gates["asc"] or gates["master"], case


def low_high(value: int, spec: tuple[str, int, int, int]) -> bool:
    _, _, low, high = spec
    return low <= value <= high


def pick(fdp: atheris.FuzzedDataProvider, valid: list[str], odd: list[str]) -> str:
    """Mostly valid values, so the fuzzer reaches the decisions, plus odd and arbitrary ones."""
    choice = fdp.ConsumeIntInRange(0, 15)
    if choice <= 13:
        return fdp.PickValueInList(valid)
    if choice == 14:
        return fdp.PickValueInList(odd)
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 16))


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    env: dict[str, str] = {}
    for name in SWITCHES:
        if fdp.ConsumeIntInRange(0, 3):
            env[name] = pick(fdp, VALID_SWITCHES, ODD_SWITCHES)
    for name, *_ in NUMBERS:
        if fdp.ConsumeBool():
            env[name] = pick(fdp, VALID_NUMBERS, ODD_NUMBERS)
    event = pick(fdp, VALID_EVENTS, ODD_EVENTS)
    marketing_version = pick(fdp, VALID_VERSIONS, ODD_VERSIONS)
    if fdp.ConsumeIntInRange(0, 3):
        ref_name = ios_release.TAG_PREFIX + marketing_version
    else:
        ref_name = ios_release.TAG_PREFIX + pick(fdp, VALID_VERSIONS, ODD_VERSIONS)
    validation_version = "" if fdp.ConsumeIntInRange(0, 3) else pick(fdp, VALID_VERSIONS, ODD_VERSIONS)
    check(env, event, ref_name, marketing_version, validation_version)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
