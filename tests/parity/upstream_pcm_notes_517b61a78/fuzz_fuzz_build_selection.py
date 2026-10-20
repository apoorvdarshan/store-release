#!/usr/bin/env python3
"""Fuzz target: App Store Connect build selection in scripts/store/ios_release.py.

Before review submission, the iOS Release workflow polls App Store Connect for the
build this run uploaded. For any builds response, `evaluate_builds` must raise
ReleaseError, keep waiting, or select a build that is provably this run's: the
only build with the expected build number, version and iOS platform, belonging to
this app, VALID, unexpired and uploaded no earlier than this run's upload (less
the allowed clock skew). It must never fall back to another build.

  python3 fuzz/fuzz_build_selection.py -max_total_time=60   # needs Atheris
"""
from __future__ import annotations

import contextlib
import datetime as dt
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts" / "store")]

try:
    import atheris
except ImportError:  # test_fuzz_targets.py replays the target without Atheris
    atheris = None

with atheris.instrument_imports() if atheris else contextlib.nullcontext():
    from ios_release import UPLOAD_CLOCK_SKEW_SECONDS, BuildState, ReleaseError, evaluate_builds

from fuzz_input import FuzzInput

APP_ID = "6792598227"
VERSION = "1.0.1"
BUILD = "42"
UPLOADED_AFTER = dt.datetime(2027, 1, 15, 9, 30, tzinfo=dt.timezone.utc).timestamp()


def upload_date(fuzz: FuzzInput) -> Any:
    choice = fuzz.choose((0, 0, 0, 1), (2, 3, 4, 5))
    if choice <= 1:
        # Recent enough (choice 0) or older than this run's upload (choice 1).
        if choice == 0:
            offset = fuzz.int_in(-UPLOAD_CLOCK_SKEW_SECONDS, 3 * 86400)
        else:
            offset = -fuzz.int_in(UPLOAD_CLOCK_SKEW_SECONDS + 1, 30 * 86400)
        suffix, minutes = fuzz.pick((("Z", 0), ("+00:00", 0), (".000Z", 0), ("-07:00", -420), ("+05:30", 330)))
        moment = dt.datetime.fromtimestamp(UPLOADED_AFTER + offset, dt.timezone(dt.timedelta(minutes=minutes)))
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + suffix
    if choice == 2:
        return fuzz.pick(("", "2027-01-15", "not a date", "2027-13-45T00:00:00Z", "2027-01-15T09:30:00+25:00"))
    if choice == 3:
        return fuzz.int_in(0, 2**31)
    if choice == 4:
        return None
    return fuzz.text(4)


def builds_payload(fuzz: FuzzInput) -> dict[str, Any]:
    included = []
    for index in range(fuzz.int_in(1, 3)):
        included.append({
            "type": fuzz.choose(("preReleaseVersions",), ("apps", "builds")),
            "id": f"pre{index}",
            "attributes": {
                "version": fuzz.choose((VERSION, VERSION, "1.0.0"), (f"{VERSION} ", None, "")),
                "platform": fuzz.choose(("IOS",), ("MAC_OS", "ios", None)),
            },
        })
    data = []
    for index in range(fuzz.int_in(0, 4)):
        attributes: dict[str, Any] = {}
        if fuzz.int_in(0, 7):
            attributes["version"] = fuzz.choose((BUILD, BUILD, "41"), ("042", None, ""))
        if fuzz.int_in(0, 7):
            attributes["uploadedDate"] = upload_date(fuzz)
        if fuzz.int_in(0, 7):
            attributes["processingState"] = fuzz.choose(("VALID", "VALID", "PROCESSING"), ("FAILED", "INVALID", "valid", None))
        if fuzz.flag():
            attributes["expired"] = fuzz.choose((False, None), (True, "false", 1))
        relationships = {
            "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": fuzz.choose(("pre0", "pre1", "pre2"), ("missing", None))}},
            "app": {"data": {"type": "apps", "id": fuzz.choose((APP_ID,), ("1234567890", None))}},
        }
        row: dict[str, Any] = {"type": "builds", "id": f"build{index}"}
        if fuzz.int_in(0, 7):
            row["attributes"] = attributes
        if fuzz.int_in(0, 7):
            row["relationships"] = relationships
        data.append(row)
    payload: dict[str, Any] = {}
    if fuzz.int_in(0, 7):
        payload["data"] = data
    if fuzz.int_in(0, 7):
        payload["included"] = included
    return payload


def identity(payload: dict[str, Any], row: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """(build number, version, platform, app id) of a builds row, read independently."""
    included = {(r["type"], r["id"]): r for r in payload.get("included") or []}
    attributes = row.get("attributes") or {}
    relationships = row.get("relationships") or {}
    pre_id = relationships.get("preReleaseVersion", {}).get("data", {}).get("id")
    pre = included.get(("preReleaseVersions", pre_id), {}).get("attributes", {})
    app_id = relationships.get("app", {}).get("data", {}).get("id")
    return attributes.get("version"), pre.get("version"), pre.get("platform"), app_id


def TestOneInput(data: bytes) -> None:
    fuzz = FuzzInput(data)
    payload = builds_payload(fuzz)
    try:
        state, message, build = evaluate_builds(
            payload, app_id=APP_ID, marketing_version=VERSION, build_number=BUILD, uploaded_after=UPLOADED_AFTER
        )
    except ReleaseError:
        return
    assert isinstance(message, str) and message
    if state == BuildState.WAITING:
        assert build is None, build
        return
    assert state == BuildState.READY, state

    rows = payload.get("data") or []
    assert any(build is row for row in rows), "selected a build that is not in the response"
    candidates = [row for row in rows if identity(payload, row)[:3] == (BUILD, VERSION, "IOS")]
    assert candidates == [build], ("selected a build that is not the only match", payload)
    assert identity(payload, build)[3] == APP_ID, ("selected another app's build", payload)
    attributes = build["attributes"]
    assert attributes.get("processingState") == "VALID", attributes
    assert attributes.get("expired") in (False, None), attributes
    uploaded = dt.datetime.fromisoformat(attributes["uploadedDate"].replace("Z", "+00:00"))
    assert uploaded.timestamp() >= UPLOADED_AFTER - UPLOAD_CLOCK_SKEW_SECONDS, attributes


def main() -> None:
    if atheris is None:
        raise SystemExit("Atheris is required: pip install --require-hashes -r fuzz/requirements.txt")
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
