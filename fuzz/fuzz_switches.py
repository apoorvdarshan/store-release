#!/usr/bin/env python3
"""Fuzz the release switches and the iOS and Android gate resolution.

    python3 -I fuzz/fuzz_switches.py [libFuzzer flags] [corpus dir]

The switches decide what a release run may upload, submit or roll out. Properties:

- the three switch readers (store_release.switches, ios.release, android.release)
  agree: true/1/yes/on and false/0/no/off/empty in any case and surrounding
  whitespace, missing is off, anything else fails; the gate summary (describe)
  shows INVALID exactly when a release would fail;
- resolve_gates raises nothing but ReleaseError, and fails on any invalid switch,
  even with the master switch off or on a validation run;
- nothing is enabled that its own switch did not ask for (production is never
  implied by a testing track), a validation run or an off master switch enables
  no store operation, iOS review submission needs this run's upload and public
  release needs submission, and Android What's New needs a selected track;
- the build wait settings are whole numbers within their bounds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402  (puts the repository root on sys.path)

import atheris  # noqa: E402

with atheris.instrument_imports(include=["store_release"]):
    from store_release import switches
    from store_release.android import release as android_release
    from store_release.ios import release as ios_release

ios_release.TAG_PREFIX = "ios-v"
android_release.TAG_PREFIX = "android-v"

TRUE = {"1", "true", "yes", "on"}
FALSE = {"", "0", "false", "no", "off"}
VALUES = ("", "true", "false", "TRUE", "False", " yes ", "on", "ON\t", "1", "0", "no", "off", " true ",
          "tru", "enabled", "2", "yes please", "true\nfalse", "t", "y", "none", "null", "ı", "Ｔrue")
EVENTS = ("push", "workflow_dispatch", "push", "workflow_dispatch", "pull_request", "schedule", "")
IOS_REFS = ("ios-v1.2.3", "ios-v1.2", "ios-v", "v1.2.3", "android-v1.2.3", "ios-v1.2.3-rc1", "refs/tags/ios-v1.2.3")
ANDROID_REFS = ("android-v3.0.1", "android-v3.0", "android-v", "v3.0.1", "ios-v3.0.1", "main")
VERSIONS = ("1.2.3", "1.2", "3.0.1", "3.0", "", "1.2.3.4", "7.2", " 7.2", "x")
NUMBERS = ("", "10", "30", "600", "9", "601", "0", "300", "301", " 45 ", "-5", "1e2", "٣٠", "0x10", "+20", "45.0")


def raw_value(fdp: atheris.FuzzedDataProvider) -> str | None:
    """One switch value: unset, a known spelling, or arbitrary text."""
    choice = fdp.ConsumeIntInRange(0, 3)
    if choice == 0:
        return None
    if choice == 1:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 10))
    return common.pick(fdp, VALUES)


def expected(raw: str | None) -> bool | None:
    """On, off, or None when the value is invalid."""
    value = (raw or "").strip().lower()
    return True if value in TRUE else False if value in FALSE else None


def check_readers(name: str, raw: str | None) -> None:
    want = expected(raw)
    for reader, error in ((switches.parse_switch, switches.SwitchError),
                          (ios_release.parse_switch, ios_release.ReleaseError),
                          (android_release.parse_switch, android_release.ReleaseError)):
        try:
            got = reader(name, raw)
        except error:
            assert want is None, (reader.__module__, raw)
        else:
            assert got is want, (reader.__module__, raw, got)
    if raw is not None and "\x00" not in raw:
        os.environ[name] = raw
        try:
            shown, on = switches.describe(name)
        finally:
            del os.environ[name]
        assert shown.startswith("INVALID") == (want is None), (raw, shown)
        assert on is bool(want), (raw, on)


def environment(fdp: atheris.FuzzedDataProvider, names: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in names:
        raw = raw_value(fdp)
        if raw is not None:
            env[name] = raw
    return env


def check_ios(fdp: atheris.FuzzedDataProvider) -> None:
    names = [ios_release.MASTER] + [name for name, _ in ios_release.OPERATION_SWITCHES]
    env = environment(fdp, names)
    for spec in (ios_release.POLL_SECONDS, ios_release.WAIT_MINUTES):
        if fdp.ConsumeBool():
            env[spec[0]] = common.pick(fdp, NUMBERS)
    event = common.pick(fdp, EVENTS)
    version = common.pick(fdp, VERSIONS)
    ref = f"ios-v{version}" if fdp.ConsumeBool() else common.pick(fdp, IOS_REFS)
    override = common.pick(fdp, ("",) + VERSIONS)
    try:
        gates = ios_release.resolve_gates(env, event=event, ref_name=ref, marketing_version=version,
                                          validation_version=override)
    except ios_release.ReleaseError:
        return
    states = {name: expected(env.get(name)) for name in names}
    assert None not in states.values(), f"an invalid switch was accepted: {states}"
    for spec in (ios_release.POLL_SECONDS, ios_release.WAIT_MINUTES):
        name, default, low, high = spec
        value = gates[{"IOS_BUILD_POLL_SECONDS": "poll_seconds", "IOS_BUILD_WAIT_MINUTES": "wait_minutes"}[name]]
        raw = (env.get(name) or "").strip()
        assert low <= value <= high or (not raw and value == default), (name, raw, value)
        assert not raw or (raw.isascii() and raw.isdigit() and int(raw) == value), (name, raw, value)
    operations = {key: gates[key] for _, key in ios_release.OPERATION_SWITCHES}
    for name, key in ios_release.OPERATION_SWITCHES:
        assert not operations[key] or states[name], f"{key} on without {name}"
    if event == "workflow_dispatch" or not states[ios_release.MASTER]:
        assert not any(operations.values()), operations
        assert not gates["asc"] and not gates["master"], gates
    if event == "workflow_dispatch":
        assert gates["mode"] == "validate" and gates["build"], gates
    else:
        assert event == "push" and ref == f"ios-v{version}", (event, ref, version)
        assert gates["mode"] == "release"
    assert not operations["submit"] or operations["upload_build"], operations
    assert not operations["public_release"] or operations["submit"], operations


def check_android(fdp: atheris.FuzzedDataProvider) -> None:
    names = [android_release.MASTER] + [name for name, _ in android_release.OPERATION_SWITCHES]
    env = environment(fdp, names)
    event = common.pick(fdp, EVENTS)
    version = common.pick(fdp, VERSIONS)
    ref = f"android-v{version}" if fdp.ConsumeBool() else common.pick(fdp, ANDROID_REFS)
    try:
        gates = android_release.resolve_gates(env, event=event, ref_name=ref, version_name=version)
    except android_release.ReleaseError:
        return
    states = {name: expected(env.get(name)) for name in names}
    assert None not in states.values(), f"an invalid switch was accepted: {states}"
    for name, key in android_release.OPERATION_SWITCHES:
        assert not gates[key] or states[name], f"{key} on without {name}"
    tracks = [track for key, track in android_release.PLAY_TRACKS if gates[key]]
    assert gates["play_tracks"] == ",".join(tracks) and gates["play_upload"] == bool(tracks), gates
    assert not gates["whats_new"] or tracks, gates
    if event == "workflow_dispatch" or not states[android_release.MASTER]:
        assert not any(gates[key] for _, key in android_release.OPERATION_SWITCHES), gates
        assert not gates["github_release"] and not gates["play_upload"], gates
    if event == "workflow_dispatch":
        assert gates["mode"] == "validate" and gates["build"], gates
    else:
        assert event == "push" and ref == f"android-v{version}", (event, ref, version)
        assert gates["build"] == gates["github_release"] == bool(states[android_release.MASTER]), gates


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    check_readers(common.pick(fdp, switches.GATES["ios"] + switches.GATES["android"]), raw_value(fdp))
    if fdp.ConsumeBool():
        check_ios(fdp)
    else:
        check_android(fdp)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
