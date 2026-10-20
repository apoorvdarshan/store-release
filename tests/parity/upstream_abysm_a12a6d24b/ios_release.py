#!/usr/bin/env python3
"""ABYSM iOS release switches, build numbering and App Store Connect build readiness.

Subcommands (all print to stdout; `gates`/`build-number`/`wait-build` also write
key=value lines to $GITHUB_OUTPUT when it is set):

  gates         Validate the IOS_* repository variables and resolve what this
                run may do. Missing values mean off; invalid values fail.
                A release tag must be exactly v<MARKETING_VERSION>. A run that
                builds also needs the app's export compliance answer.
  export-compliance
                Check the app's export compliance answer
                (ITSAppUsesNonExemptEncryption) in the project, or with
                --info-plist in an archived app's Info.plist.
  build-number  Next CFBundleVersion: max(project value, highest ASC build + 1).
  wait-build    Poll App Store Connect until the build uploaded by THIS run is
                processed (VALID, unexpired, matching app/version/build number,
                uploaded after this run's upload started). Fails on timeout,
                processing failure, ambiguity or mismatch and never falls back to
                another build; a build without an upload time is never accepted.

See store/README.md for what each switch controls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import os
import plistlib
import re
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[2]
PBXPROJ = ROOT / "Abysm.xcodeproj" / "project.pbxproj"
BUNDLE_ID = "com.apoorvdarshan.Abysm"
# App Store Connect app record for BUNDLE_ID ("ABYSM - Dive the Deep Sea").
APP_STORE_APP_ID = "6783505799"
TAG_PREFIX = "v"

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}

MASTER = "IOS_RELEASE_AUTOMATION"
OPERATION_SWITCHES = (
    ("IOS_CREATE_VERSION", "create_version"),
    ("IOS_UPLOAD_BUILD", "upload_build"),
    ("IOS_UPLOAD_LISTING", "listing"),
    ("IOS_UPLOAD_WHATS_NEW", "whats_new"),
    ("IOS_UPLOAD_SCREENSHOTS", "screenshots"),
    ("IOS_UPLOAD_CREATIVE_ASSETS", "creative"),
    ("IOS_SUBMIT_REVIEW", "submit"),
    ("IOS_PUBLIC_RELEASE", "public_release"),
)
POLL_SECONDS = ("IOS_BUILD_POLL_SECONDS", 30, 10, 600)
WAIT_MINUTES = ("IOS_BUILD_WAIT_MINUTES", 120, 10, 300)

# Builds this old (relative to this run's upload) cannot be the one we uploaded.
UPLOAD_CLOCK_SKEW_SECONDS = 600
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}

# App Store versions: 1.0, 1.0.1, … (ASCII digits only).
VERSION_PATTERN = re.compile(r"[0-9]+(\.[0-9]+){0,2}")


class ReleaseError(Exception):
    """A release precondition failed; the run must stop without publishing."""


def parse_switch(name: str, raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ReleaseError(
        f"{name}={raw!r} is not a valid switch value; use true/false (or leave it unset)"
    )


def parse_bounded_int(spec: tuple[str, int, int, int], raw: str | None) -> int:
    name, default, low, high = spec
    value = (raw or "").strip()
    if not value:
        return default
    if not re.fullmatch(r"[0-9]+", value):
        raise ReleaseError(f"{name}={raw!r} must be a whole number between {low} and {high}")
    number = int(value)
    if not low <= number <= high:
        raise ReleaseError(f"{name}={number} must be between {low} and {high}")
    return number


_CONFIG_BLOCK = re.compile(
    r"isa = XCBuildConfiguration;\s*buildSettings = \{(?P<settings>.*?)\n\t\t\t\};\s*name = (?P<name>[^;]+);",
    re.S,
)
_SETTING_LINE = re.compile(r"^\s*([A-Za-z0-9_\[\]=*]+) = (.*);\s*$")


def app_build_configurations(
    pbxproj: Path = PBXPROJ, bundle_id: str = BUNDLE_ID
) -> dict[str, dict[str, str]]:
    """Build settings of every configuration that builds `bundle_id` (the shipped app).

    Test targets keep their own versions; only the app's values matter for a release.
    """
    text = pbxproj.read_text(encoding="utf-8")
    configs: dict[str, dict[str, str]] = {}
    for match in _CONFIG_BLOCK.finditer(text):
        settings: dict[str, str] = {}
        for line in match.group("settings").splitlines():
            found = _SETTING_LINE.match(line)
            if found:
                settings[found.group(1)] = found.group(2).strip().strip('"')
        if settings.get("PRODUCT_BUNDLE_IDENTIFIER") == bundle_id:
            name = match.group("name").strip().strip('"')
            if name in configs:
                raise ReleaseError(f"{bundle_id} has two {name!r} build configurations")
            configs[name] = settings
    if not configs:
        raise ReleaseError(f"no build configuration builds {bundle_id} in {pbxproj.name}")
    return configs


def app_build_setting(setting: str, pbxproj: Path = PBXPROJ, bundle_id: str = BUNDLE_ID) -> str:
    """One value for `setting`, identical in every configuration of the app target."""
    configs = app_build_configurations(pbxproj, bundle_id)
    values = {name: settings.get(setting) for name, settings in configs.items()}
    distinct = set(values.values())
    if None in distinct or len(distinct) != 1:
        raise ReleaseError(
            f"{setting} must be set to one value in every {bundle_id} configuration; found {values}"
        )
    return distinct.pop()  # type: ignore[return-value]


def project_marketing_version(pbxproj: Path = PBXPROJ) -> str:
    version = app_build_setting("MARKETING_VERSION", pbxproj)
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseError(f"MARKETING_VERSION {version!r} must look like 1.0 or 1.0.1")
    return version


def project_build_number(pbxproj: Path = PBXPROJ) -> int:
    build = app_build_setting("CURRENT_PROJECT_VERSION", pbxproj)
    if not re.fullmatch(r"[0-9]+", build):
        raise ReleaseError(f"CURRENT_PROJECT_VERSION must be a whole number, got {build!r}")
    return int(build)


# Export compliance. Without ITSAppUsesNonExemptEncryption, App Store Connect asks
# about every build by hand and review submission fails (HTTP 409,
# usesNonExemptEncryption missing) until someone answers. NO means only exempt
# encryption (HTTPS, Keychain, hashing, signature checks, Apple's system crypto);
# YES also needs the code App Store Connect issues for the encryption documentation.
ENCRYPTION_KEY = "ITSAppUsesNonExemptEncryption"
COMPLIANCE_CODE_KEY = "ITSEncryptionExportComplianceCode"
ENCRYPTION_SETTING = f"INFOPLIST_KEY_{ENCRYPTION_KEY}"
COMPLIANCE_CODE_SETTING = f"INFOPLIST_KEY_{COMPLIANCE_CODE_KEY}"


def _plist_answer(value: Any) -> str:
    """YES/NO for an Info.plist Boolean; anything else is shown as found (and rejected)."""
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return repr(value)


def _load_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ReleaseError(f"{path} is not an Info.plist dictionary")
    return data


def decide_export_compliance(answers: Mapping[str, list[str]], codes: Mapping[str, bool], *, where: str) -> str:
    """The one answer every entry of `answers` (configuration or bundle → declared values) gives."""
    missing = [name for name, found in answers.items() if not found]
    if not answers or missing:
        raise ReleaseError(
            f"{where} does not declare export compliance ({', '.join(missing) or 'nothing found'}): set "
            f"{ENCRYPTION_KEY} to NO (only HTTPS, Keychain or Apple's system crypto) or to YES with "
            f"{COMPLIANCE_CODE_KEY}"
        )
    values = {value for found in answers.values() for value in found}
    if len(values) != 1 or not values <= {"NO", "YES"}:
        raise ReleaseError(f"{ENCRYPTION_KEY} must be one NO or YES everywhere in {where}; found {dict(answers)}")
    answer = values.pop()
    lacking = [name for name in answers if not codes.get(name)]
    if answer == "YES" and lacking:
        raise ReleaseError(
            f"{ENCRYPTION_KEY} = YES needs {COMPLIANCE_CODE_KEY} (the code App Store Connect issues for the "
            f"app's encryption documentation); missing in {where}: {', '.join(lacking)}"
        )
    return answer


def check_export_compliance(pbxproj: Path = PBXPROJ, bundle_id: str = BUNDLE_ID) -> str:
    """The app's export compliance answer, read from the project before anything is built.

    Each configuration of the app target (Debug and Release, both required) is
    read on its own, so a value set only for Debug or only on another target
    never counts for the Release build that ships. Each must give the same
    answer, from its INFOPLIST_KEY_ITSAppUsesNonExemptEncryption build setting or
    its Info.plist file (ABYSM uses Abysm/Info.plist); both together must agree,
    and YES needs the compliance code in that same configuration.
    """
    configurations = app_build_configurations(pbxproj, bundle_id)
    if not {"Debug", "Release"} <= configurations.keys():
        raise ReleaseError(
            f"could not find the Debug and Release configurations of the {bundle_id} target "
            f"(found {sorted(configurations)}); export compliance must be checked in both"
        )
    source_root = pbxproj.parent.parent
    answers: dict[str, list[str]] = {}
    codes: dict[str, bool] = {}
    for name, settings in configurations.items():
        found: list[str] = []
        has_code = bool(settings.get(COMPLIANCE_CODE_SETTING, "").strip())
        if ENCRYPTION_SETTING in settings:
            found.append(settings[ENCRYPTION_SETTING])
        plist_setting = settings.get("INFOPLIST_FILE", "")
        if plist_setting:
            relative = plist_setting.removeprefix("$(SRCROOT)/").removeprefix("${SRCROOT}/")
            if "$" in relative:
                raise ReleaseError(f"{name}: cannot resolve INFOPLIST_FILE {plist_setting!r} to check export compliance")
            data = _load_plist(source_root / relative)
            if ENCRYPTION_KEY in data:
                found.append(_plist_answer(data[ENCRYPTION_KEY]))
            has_code = has_code or bool(str(data.get(COMPLIANCE_CODE_KEY, "")).strip())
        answers[name] = found
        codes[name] = has_code
    return decide_export_compliance(answers, codes, where=f"the app target ({bundle_id})")


def check_bundle_export_compliance(info_plist: Path) -> str:
    """The export compliance answer in a built app's Info.plist (checked after the archive)."""
    data = _load_plist(info_plist)
    bundle = info_plist.parent.name or str(info_plist)
    found = [_plist_answer(data[ENCRYPTION_KEY])] if ENCRYPTION_KEY in data else []
    has_code = bool(str(data.get(COMPLIANCE_CODE_KEY, "")).strip())
    return decide_export_compliance({bundle: found}, {bundle: has_code}, where=f"the archived {bundle}")


def describe_export_compliance(answer: str) -> str:
    detail = "no non-exempt encryption" if answer == "NO" else "non-exempt encryption with an export compliance code"
    return f"export compliance: {ENCRYPTION_KEY} = {answer} ({detail})"


def resolve_gates(
    env: Mapping[str, str],
    *,
    event: str,
    ref_name: str,
    marketing_version: str,
    validation_version: str = "",
) -> dict[str, Any]:
    """Return what this run is allowed to do. Raises ReleaseError on bad config."""
    if event not in {"push", "workflow_dispatch"}:
        raise ReleaseError(f"unsupported event {event!r}")
    validation_version = validation_version.strip()
    if validation_version:
        if event != "workflow_dispatch":
            raise ReleaseError("a version override only applies to manual validation runs")
        if not VERSION_PATTERN.fullmatch(validation_version):
            raise ReleaseError(
                f"validation version {validation_version!r} must look like 1.0.1 or 1.1"
            )

    # Parse every switch first so a typo fails even when the master is off.
    master = parse_switch(MASTER, env.get(MASTER))
    requested = {key: parse_switch(name, env.get(name)) for name, key in OPERATION_SWITCHES}
    poll_seconds = parse_bounded_int(POLL_SECONDS, env.get(POLL_SECONDS[0]))
    wait_minutes = parse_bounded_int(WAIT_MINUTES, env.get(WAIT_MINUTES[0]))
    notes: list[str] = []

    if event == "workflow_dispatch":
        # Manual runs only prove the build archives, signs, exports and validates.
        resolved = {key: False for _, key in OPERATION_SWITCHES}
        mode = "validate"
        notes.append(
            "validation run: the build is archived, signed and validated only; "
            "nothing is uploaded, listed, submitted or released"
        )
        if validation_version:
            notes.append(
                f"validating as version {validation_version} (project is {marketing_version}); "
                "the project files are not changed"
            )
            marketing_version = validation_version
    else:
        if not ref_name.startswith(TAG_PREFIX):
            raise ReleaseError(f"release runs need a {TAG_PREFIX}<version> tag, got {ref_name!r}")
        tag_version = ref_name[len(TAG_PREFIX):]
        if tag_version != marketing_version:
            raise ReleaseError(
                f"tag {ref_name} does not match MARKETING_VERSION {marketing_version}; "
                f"release tags must be exactly {TAG_PREFIX}{marketing_version} "
                "(fix the tag or bump the version before releasing)"
            )
        mode = "release"
        if master:
            if requested["submit"] and not requested["upload_build"]:
                raise ReleaseError(
                    "IOS_SUBMIT_REVIEW needs IOS_UPLOAD_BUILD: review submission only uses "
                    "the build uploaded by this run, never an older build"
                )
            if requested["public_release"] and not requested["submit"]:
                raise ReleaseError("IOS_PUBLIC_RELEASE only applies with IOS_SUBMIT_REVIEW on")
            resolved = dict(requested)
        else:
            ignored = [name for name, key in OPERATION_SWITCHES if requested[key]]
            if ignored:
                notes.append(f"{MASTER} is off, so these are ignored: {', '.join(ignored)}")
            resolved = {key: False for _, key in OPERATION_SWITCHES}

    resolved.update(
        {
            "mode": mode,
            "master": master and mode == "release",
            "build": mode == "validate" or resolved["upload_build"],
            "asc": any(resolved[k] for k in ("listing", "whats_new", "screenshots", "creative", "submit")),
            "poll_seconds": poll_seconds,
            "wait_minutes": wait_minutes,
            "marketing_version": marketing_version,
        }
    )
    resolved["notes"] = notes
    return resolved


def next_build_number(project_build: int, asc_build_versions: list[str]) -> int:
    numeric = [int(v) for v in asc_build_versions if re.fullmatch(r"[0-9]+", v or "")]
    highest = max(numeric, default=0)
    return max(project_build, highest + 1)


def parse_asc_date(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class BuildState:
    WAITING = "waiting"
    READY = "ready"


def evaluate_builds(
    payload: dict[str, Any],
    *,
    app_id: str,
    marketing_version: str,
    build_number: str,
    uploaded_after: float,
) -> tuple[str, str, dict[str, Any] | None]:
    """Classify one poll. Returns (state, message, build). Raises on hard failures."""
    included = {
        (row.get("type"), row.get("id")): row for row in payload.get("included") or []
    }
    matches = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        rels = row.get("relationships") or {}
        pre_ref = ((rels.get("preReleaseVersion") or {}).get("data")) or {}
        app_ref = ((rels.get("app") or {}).get("data")) or {}
        pre = included.get(("preReleaseVersions", pre_ref.get("id"))) or {}
        pre_attrs = pre.get("attributes") or {}
        if attrs.get("version") != build_number:
            continue
        if pre_attrs.get("version") != marketing_version or pre_attrs.get("platform") != "IOS":
            continue
        if app_ref.get("id") != app_id:
            raise ReleaseError(f"build {row.get('id')} belongs to another app ({app_ref.get('id')})")
        matches.append(row)

    if not matches:
        return BuildState.WAITING, "build not visible in App Store Connect yet", None
    if len(matches) > 1:
        ids = ", ".join(str(m.get("id")) for m in matches)
        raise ReleaseError(
            f"ambiguous: {len(matches)} builds match {marketing_version} ({build_number}): {ids}"
        )

    build = matches[0]
    attrs = build.get("attributes") or {}
    uploaded = attrs.get("uploadedDate")
    if uploaded:
        try:
            uploaded_at = parse_asc_date(uploaded)
        except (AttributeError, TypeError, ValueError):
            raise ReleaseError(
                f"build {build_number} has an unreadable uploadedDate {uploaded!r}; "
                "cannot prove it is the build this run uploaded"
            ) from None
        if uploaded_at < uploaded_after - UPLOAD_CLOCK_SKEW_SECONDS:
            raise ReleaseError(
                f"build {build_number} was uploaded at {uploaded}, before this run's upload; "
                "refusing to use an older build"
            )
    state = attrs.get("processingState")
    if state in {"FAILED", "INVALID"}:
        raise ReleaseError(f"App Store Connect processing ended in {state} for build {build_number}")
    if state != "VALID":
        return BuildState.WAITING, f"processing state {state or 'unknown'}", None
    if attrs.get("expired"):
        raise ReleaseError(f"build {build_number} is expired")
    if not uploaded:
        # Fail closed: without an upload time this could be an older build with the
        # same number. Keep waiting (it times out at the deadline), never pass.
        return BuildState.WAITING, "processed, but App Store Connect reports no uploadedDate yet", None
    return BuildState.READY, f"build {build_number} is VALID", build


def builds_query(app_id: str, build_number: str) -> str:
    return "/builds?" + urllib.parse.urlencode(
        {
            "filter[app]": app_id,
            "filter[version]": build_number,
            "include": "preReleaseVersion,app",
            "fields[builds]": "version,processingState,expired,uploadedDate,preReleaseVersion,app",
            "fields[preReleaseVersions]": "version,platform",
            "limit": "20",
        }
    )


def wait_for_build(
    client: Any,
    *,
    app_id: str,
    marketing_version: str,
    build_number: str,
    uploaded_after: float,
    deadline: float,
    poll_seconds: int,
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    is_transient: Callable[[BaseException], bool] | None = None,
) -> dict[str, Any]:
    """Poll until this run's build is processed; raise ReleaseError otherwise."""
    now = now or time.time
    sleep = sleep or time.sleep
    path = builds_query(app_id, build_number)
    while True:
        # The budget is total (upload + processing): a build first seen as ready
        # after the deadline is still a timeout, never a late success.
        if now() > deadline:
            raise ReleaseError(
                f"timed out waiting for {marketing_version} ({build_number}) to finish processing"
            )
        try:
            payload = client.get(path)
        except BaseException as exc:  # noqa: BLE001 - classified below
            if is_transient is None or not is_transient(exc):
                raise
            state, message, build = (
                BuildState.WAITING, f"transient App Store Connect error: {exc}", None
            )
        else:
            state, message, build = evaluate_builds(
                payload,
                app_id=app_id,
                marketing_version=marketing_version,
                build_number=build_number,
                uploaded_after=uploaded_after,
            )
        if state == BuildState.READY and build is not None:
            print(f"  {message} (id {build['id']})")
            return build
        remaining = deadline - now()
        print(f"  waiting: {message}; {max(0, int(remaining))}s left")
        if remaining <= 0:
            raise ReleaseError(
                f"timed out waiting for {marketing_version} ({build_number}) to finish processing"
            )
        sleep(min(poll_seconds, max(1.0, remaining)))


def is_transient_error(exc: BaseException) -> bool:
    """Network hiccups and App Store Connect overload are retried; anything else stops."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUSES
    status = getattr(exc, "status", None)
    if isinstance(exc, SystemExit):  # asc_release.AscHttpError carries the HTTP status
        return isinstance(status, int) and status in TRANSIENT_HTTP_STATUSES
    return isinstance(
        exc, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)
    )


def call_with_retries(
    fn: Callable[[], Any],
    *,
    deadline: float,
    poll_seconds: int,
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    is_transient: Callable[[BaseException], bool] = is_transient_error,
) -> Any:
    now = now or time.time
    sleep = sleep or time.sleep
    while True:
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - classified below
            remaining = deadline - now()
            if not is_transient(exc) or remaining <= 0:
                raise
            print(f"  transient App Store Connect error ({exc!r}); retrying")
            sleep(min(poll_seconds, max(1.0, remaining)))


def write_outputs(values: Mapping[str, Any]) -> None:
    lines = []
    for key, value in values.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key}={value}")
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    for line in lines:
        print(f"  {line}")


def asc_client() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import asc_release  # noqa: PLC0415 - keeps `gates` free of PyJWT

    return asc_release, asc_release.client_from_environment()


def cmd_gates(_: argparse.Namespace) -> None:
    resolved = resolve_gates(
        os.environ,
        event=os.environ.get("GITHUB_EVENT_NAME", ""),
        ref_name=os.environ.get("GITHUB_REF_NAME", ""),
        marketing_version=project_marketing_version(),
        validation_version=os.environ.get("VALIDATION_MARKETING_VERSION", ""),
    )
    notes = resolved.pop("notes")
    if resolved["build"]:
        # Before anything is built: App Store Connect cannot take the build for
        # review without the answer, and the archive check repeats this.
        notes.append(describe_export_compliance(check_export_compliance()))
    print("iOS release switches:")
    for note in notes:
        print(f"  note: {note}")
    write_outputs(resolved)


def cmd_export_compliance(args: argparse.Namespace) -> None:
    answer = check_bundle_export_compliance(args.info_plist) if args.info_plist else check_export_compliance()
    print(describe_export_compliance(answer))


def cmd_build_number(_: argparse.Namespace) -> None:
    project_build = project_build_number()
    asc_release, client = asc_client()
    app_id = asc_release.find_app(client, BUNDLE_ID)
    rows = asc_release.iter_collection(
        client,
        "/builds?" + urllib.parse.urlencode(
            {"filter[app]": app_id, "fields[builds]": "version", "limit": "200"}
        ),
    )
    versions = [(row.get("attributes") or {}).get("version", "") for row in rows]
    number = next_build_number(project_build, versions)
    print(f"project build {project_build}; {len(versions)} uploaded builds; next build {number}")
    write_outputs({"build_number": number})


def cmd_wait_build(args: argparse.Namespace) -> None:
    asc_release, client = asc_client()
    # The budget covers upload + processing, so it is counted from upload start.
    deadline = args.started_at + args.wait_minutes * 60
    print(
        f"waiting for {args.version} ({args.build}) — poll every {args.poll_seconds}s, "
        f"deadline {dt.datetime.fromtimestamp(deadline, dt.timezone.utc).isoformat()}"
    )
    app_id = call_with_retries(
        lambda: asc_release.find_app(client, BUNDLE_ID),
        deadline=deadline,
        poll_seconds=args.poll_seconds,
    )
    build = wait_for_build(
        client,
        app_id=app_id,
        marketing_version=args.version,
        build_number=str(args.build),
        uploaded_after=args.started_at,
        deadline=deadline,
        poll_seconds=args.poll_seconds,
        is_transient=is_transient_error,
    )
    write_outputs({"build_id": build["id"]})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gates").set_defaults(func=cmd_gates)
    sub.add_parser("build-number").set_defaults(func=cmd_build_number)
    compliance = sub.add_parser("export-compliance")
    compliance.add_argument("--info-plist", type=Path, help="an archived app's Info.plist (default: check the project)")
    compliance.set_defaults(func=cmd_export_compliance)
    wait = sub.add_parser("wait-build")
    wait.add_argument("--version", required=True)
    wait.add_argument("--build", required=True, type=int)
    wait.add_argument(
        "--started-at", required=True, type=float, help="epoch seconds when this run began uploading"
    )
    wait.add_argument("--wait-minutes", required=True, type=int)
    wait.add_argument("--poll-seconds", required=True, type=int)
    wait.set_defaults(func=cmd_wait_build)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
