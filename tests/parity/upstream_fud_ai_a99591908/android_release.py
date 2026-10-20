#!/usr/bin/env python3
"""Android release switches, artifact checks and Play track read-back.

Subcommands (`gates` also writes key=value lines to $GITHUB_OUTPUT when it is set):

  gates           Validate the ANDROID_* repository variables and resolve what this
                  run may do. Missing values mean off; invalid values fail. A tag
                  run needs android-v<versionName>; a manual run only validates.
  github-preflight
                  Read-only: this versionCode must be higher than that of every
                  earlier android-v* GitHub Release (from their titles), so the
                  published APK installs over them.
  play-preflight  Read-only: compare this versionCode with everything Google Play
                  already has. New (higher than every uploaded versionCode): the
                  upload goes ahead. Already completed on every selected track (a
                  re-run after an earlier attempt reached Play): the upload is
                  skipped. Anything else fails before the build or upload.
  write-signing-properties
                  Write the Gradle keystore.properties for the upload key under
                  $RUNNER_TEMP (mode 600, Java properties escaping).
  verify          Before anything leaves the runner, check the built AAB and APK:
                  package name, versionName/versionCode from build.gradle.kts, and
                  that both are signed only by the pinned upload certificate.
  check-tracks    After a Play upload, read every selected track back and confirm
                  it carries this versionCode as a completed release. Never edits.

See store/README.md for what each switch controls.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
PACKAGE_NAME = "com.apoorvdarshan.calorietracker"
TAG_PREFIX = "android-v"

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}

MASTER = "ANDROID_RELEASE_AUTOMATION"
OPERATION_SWITCHES = (
    ("ANDROID_UPLOAD_LISTING", "listing"),
    ("ANDROID_UPLOAD_WHATS_NEW", "whats_new"),
    ("ANDROID_UPLOAD_SCREENSHOTS", "screenshots"),
    ("ANDROID_INTERNAL_TESTING", "internal"),
    ("ANDROID_CLOSED_TESTING", "closed"),
    ("ANDROID_PRODUCTION_ROLLOUT", "production"),
)
# Switch key -> Google Play track id. Closed testing is Play's default "alpha" track.
PLAY_TRACKS = (("internal", "internal"), ("closed", "alpha"), ("production", "production"))
TRACK_LABELS = {"internal": "internal testing", "alpha": "closed testing", "production": "production"}

# SHA-256 of the Play upload certificate (alias fudai in fudai-release.jks). Every
# release since 1.0.0 is signed with it. `verify` fails when the keystore in the
# ANDROID_KEYSTORE_BASE64 secret holds any other certificate, so a swapped secret
# can never sign or publish an APK. Change it only deliberately, after a Play
# upload-key reset (see store/README.md).
UPLOAD_CERT_SHA256 = "35d739f426248c3c04f344ffaaf972dabc67b9aaa64f49143ef8f3f4b1f704e0"

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
PEM_CERT = re.compile(
    r"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----", re.DOTALL
)


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


def gradle_version(text: str) -> tuple[str, int]:
    names = re.findall(r'^\s*versionName = "([^"]+)"', text, flags=re.MULTILINE)
    codes = re.findall(r"^\s*versionCode = ([0-9]+)\s*$", text, flags=re.MULTILINE)
    if len(names) != 1 or len(codes) != 1:
        raise ReleaseError(
            "android/app/build.gradle.kts must declare exactly one versionName and one versionCode"
        )
    return names[0], int(codes[0])


def resolve_gates(
    env: Mapping[str, str], *, event: str, ref_name: str, version_name: str
) -> dict[str, Any]:
    """Return what this run is allowed to do. Raises ReleaseError on bad config."""
    if event not in {"push", "workflow_dispatch"}:
        raise ReleaseError(f"unsupported event {event!r}")
    # Parse every switch first so a typo fails even when the master is off.
    master = parse_switch(MASTER, env.get(MASTER))
    requested = {key: parse_switch(name, env.get(name)) for name, key in OPERATION_SWITCHES}
    notes: list[str] = []

    if event == "workflow_dispatch":
        # Manual runs only prove the signed build is the right app, version and key.
        mode = "validate"
        resolved = {key: False for _, key in OPERATION_SWITCHES}
        notes.append(
            "validation run: the signed AAB and APK are built and verified only; nothing is "
            "uploaded, signed with Sigstore or released"
        )
    else:
        if not ref_name.startswith(TAG_PREFIX):
            raise ReleaseError(f"release runs need an {TAG_PREFIX}* tag, got {ref_name!r}")
        if ref_name[len(TAG_PREFIX):] != version_name:
            raise ReleaseError(
                f"tag {ref_name} does not match versionName {version_name}; "
                "fix the tag or bump the version before releasing"
            )
        mode = "release"
        if master:
            resolved = dict(requested)
        else:
            notes.append(f"{MASTER} is off: no signed build, GitHub Release or Play operation runs")
            ignored = [name for name, key in OPERATION_SWITCHES if requested[key]]
            if ignored:
                notes.append(f"ignored because {MASTER} is off: {', '.join(ignored)}")
            resolved = {key: False for _, key in OPERATION_SWITCHES}

    tracks = [track for key, track in PLAY_TRACKS if resolved[key]]
    if resolved["whats_new"] and not tracks:
        notes.append(
            "ANDROID_UPLOAD_WHATS_NEW is on but no Play track is selected, so there is no "
            "release to attach What's New to; it is skipped"
        )
        resolved["whats_new"] = False

    published = mode == "release" and master
    resolved.update(
        {
            "mode": mode,
            "build": mode == "validate" or published,
            "github_release": published,
            "play_upload": bool(tracks),
            "play_tracks": ",".join(tracks),
        }
    )
    resolved["notes"] = notes
    return resolved


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


# --- artifact checks ---------------------------------------------------------


def pem_fingerprints(text: str) -> set[str]:
    """SHA-256 fingerprints (lowercase hex) of every PEM certificate in text."""
    prints = set()
    for body in PEM_CERT.findall(text):
        der = base64.b64decode("".join(body.split()))
        prints.add(hashlib.sha256(der).hexdigest())
    return prints


def apksigner_fingerprints(output: str) -> set[str]:
    """Signer certificate digests from `apksigner verify --print-certs`.

    Older build-tools print "Signer #1 certificate SHA-256 digest", newer ones
    "V2 Signer: certificate SHA-256 digest". A source stamp is not an app signer.
    """
    prints = set()
    for line in output.splitlines():
        if "Source Stamp" in line:
            continue
        match = re.search(r"certificate SHA-256 digest: ([0-9a-fA-F]{64})\s*$", line)
        if match:
            prints.add(match.group(1).lower())
    return prints


def badging_identity(output: str) -> tuple[str, int, str]:
    match = re.search(
        r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']*)'",
        output,
        flags=re.MULTILINE,
    )
    if not match:
        raise ReleaseError("could not read package/versionCode/versionName from the APK")
    return match.group(1), int(match.group(2)), match.group(3)


def manifest_identity(xml_text: str) -> tuple[str, int, str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ReleaseError(f"could not parse the AAB manifest: {exc}") from None
    package = root.get("package") or ""
    code = root.get(f"{ANDROID_NS}versionCode") or ""
    name = root.get(f"{ANDROID_NS}versionName") or ""
    if not package or not re.fullmatch(r"[0-9]+", code):
        raise ReleaseError("could not read package/versionCode from the AAB manifest")
    return package, int(code), name


SIGNATURE_FILE = re.compile(r"META-INF/(MANIFEST\.MF|[^/]+\.(SF|RSA|DSA|EC)|SIG-[^/]+)")


def manifest_entry_names(manifest: str) -> set[str]:
    """Entry names listed in a JAR manifest (unwrapping 72-byte continuation lines)."""
    unwrapped = re.sub(r"\r?\n ", "", manifest)
    return {
        line[len("Name: "):].strip()
        for line in unwrapped.splitlines()
        if line.startswith("Name: ")
    }


def unsigned_entries(archive: Path) -> list[str]:
    """Files in a JAR-signed archive (an AAB) that its signature does not cover.

    `jarsigner -verify` checks every entry the manifest lists, but still reports
    "jar verified." when other entries were added without a signature.
    """
    with zipfile.ZipFile(archive) as bundle:
        names = [n for n in bundle.namelist() if not n.endswith("/")]
        if "META-INF/MANIFEST.MF" not in names:
            return [n for n in names if not SIGNATURE_FILE.fullmatch(n)]
        listed = manifest_entry_names(bundle.read("META-INF/MANIFEST.MF").decode("utf-8"))
    return sorted(n for n in names if not SIGNATURE_FILE.fullmatch(n) and n not in listed)


def check_identity(
    kind: str, found: tuple[str, int, str], *, package: str, version_code: int, version_name: str
) -> None:
    expected = (package, version_code, version_name)
    if found != expected:
        raise ReleaseError(
            f"{kind} is {found[0]} {found[2]} ({found[1]}), expected "
            f"{package} {version_name} ({version_code})"
        )


def check_upload_certificate(found: str, expected: str) -> None:
    """The keystore's certificate must be the pinned upload certificate."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise ReleaseError(f"pinned upload certificate {expected!r} is not a SHA-256 hex digest")
    if found != expected:
        raise ReleaseError(
            f"the keystore's upload certificate is {found}, not the pinned {expected}; "
            "check ANDROID_KEYSTORE_BASE64 (update UPLOAD_CERT_SHA256 only after a Play upload-key reset)"
        )


def check_signers(kind: str, found: set[str], *, upload_cert: str) -> None:
    if not found:
        raise ReleaseError(f"{kind} is not signed")
    if found != {upload_cert}:
        raise ReleaseError(
            f"{kind} must be signed only by the upload key ({upload_cert}); found {sorted(found)}"
        )


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ReleaseError(
            f"{Path(cmd[0]).name} failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
        )
    return result


def build_tool(name: str) -> str:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        raise ReleaseError("ANDROID_HOME is not set; cannot find build-tools")

    def version_key(path: Path) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"[0-9]+", path.parent.name))

    candidates = sorted(Path(sdk, "build-tools").glob(f"*/{name}"), key=version_key)
    if not candidates:
        raise ReleaseError(f"{name} not found under {sdk}/build-tools")
    return str(candidates[-1])


def cmd_verify(args: argparse.Namespace) -> None:
    version_name, version_code = gradle_version(GRADLE.read_text(encoding="utf-8"))
    identity = {"package": args.package, "version_code": version_code, "version_name": version_name}

    exported = run(
        [
            "keytool", "-exportcert", "-rfc", "-alias", args.alias, "-keystore", str(args.keystore),
            "-storepass:env", "ANDROID_KEYSTORE_PASSWORD",
        ]
    )
    upload_certs = pem_fingerprints(exported.stdout)
    if len(upload_certs) != 1:
        raise ReleaseError(f"expected one upload certificate in the keystore, found {len(upload_certs)}")
    upload_cert = upload_certs.pop()
    print(f"upload certificate SHA-256: {upload_cert}")
    check_upload_certificate(upload_cert, args.expected_cert)

    badging = run([build_tool("aapt2"), "dump", "badging", str(args.apk)])
    check_identity("APK", badging_identity(badging.stdout), **identity)
    signers = run([build_tool("apksigner"), "verify", "--verbose", "--print-certs", str(args.apk)])
    check_signers("APK", apksigner_fingerprints(signers.stdout), upload_cert=upload_cert)

    manifest = run(["java", "-jar", str(args.bundletool), "dump", "manifest", "--bundle", str(args.aab)])
    check_identity("AAB", manifest_identity(manifest.stdout), **identity)
    jar = run(["jarsigner", "-verify", str(args.aab)], check=False)
    if jar.returncode != 0 or "jar verified." not in jar.stdout:
        raise ReleaseError(f"AAB signature does not verify: {(jar.stdout + jar.stderr).strip()[-2000:]}")
    unsigned = unsigned_entries(args.aab)
    if unsigned:
        raise ReleaseError(
            f"AAB has {len(unsigned)} entr{'y' if len(unsigned) == 1 else 'ies'} its signature "
            f"does not cover, e.g. {unsigned[0]}"
        )
    printed = run(["keytool", "-printcert", "-rfc", "-jarfile", str(args.aab)])
    check_signers("AAB", pem_fingerprints(printed.stdout), upload_cert=upload_cert)

    print(
        f"verified AAB and APK: {args.package} {version_name} ({version_code}), "
        "signed only by the pinned upload key"
    )


# --- Play read-back ----------------------------------------------------------


def track_release_state(track: dict[str, Any], version_code: int) -> str:
    """Status of the release on this track that carries version_code ('' if none)."""
    for release in track.get("releases") or []:
        if str(version_code) in (release.get("versionCodes") or []):
            return release.get("status") or ""
    return ""


def check_tracks(fetch_track, tracks: list[str], version_code: int) -> list[str]:
    """Confirm each track carries version_code as a completed release."""
    messages = []
    for track in tracks:
        state = track_release_state(fetch_track(track), version_code)
        label = TRACK_LABELS.get(track, track)
        if state != "completed":
            found = state or "no release"
            raise ReleaseError(
                f"Play {label} ({track}) does not carry versionCode {version_code} as a completed "
                f"release (found: {found})"
            )
        messages.append(
            f"Play {label}: versionCode {version_code} uploaded and assigned (status completed). "
            "Google Play may still review it before it reaches "
            + ("users" if track == "production" else "testers")
        )
    return messages


def parse_tracks(raw: str) -> list[str]:
    """The selected Play track ids. Empty, unknown or repeated ids are refused: the
    pinned upload action falls back to production when it gets no track."""
    tracks = [t for t in (raw or "").split(",") if t]
    if not tracks:
        raise ReleaseError("no Play track selected; refusing to call Play (it would default to production)")
    unknown = [t for t in tracks if t not in TRACK_LABELS]
    if unknown:
        raise ReleaseError(f"unexpected Play track(s): {', '.join(unknown)}")
    if len(set(tracks)) != len(tracks):
        raise ReleaseError(f"Play track listed twice: {raw}")
    return tracks


def play_publisher() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import play_listing  # noqa: PLC0415 - keeps `gates` free of Google API deps

    raw = os.environ.get("PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise ReleaseError("PLAY_SERVICE_ACCOUNT_JSON is not set")
    return play_listing.build_android_publisher(json.loads(raw))


def read_play_state(service: Any, reader) -> Any:
    """Run reader(edits, edit_id) in a throwaway edit that is deleted, never committed."""
    edits = service.edits()
    edit_id = edits.insert(packageName=PACKAGE_NAME, body={}).execute()["id"]
    try:
        return reader(edits, edit_id)
    finally:
        edits.delete(packageName=PACKAGE_NAME, editId=edit_id).execute()


def cmd_check_tracks(args: argparse.Namespace) -> None:
    tracks = parse_tracks(args.tracks)
    _, version_code = gradle_version(GRADLE.read_text(encoding="utf-8"))

    def reader(edits, edit_id):
        return check_tracks(
            lambda track: edits.tracks().get(packageName=PACKAGE_NAME, editId=edit_id, track=track).execute(),
            tracks,
            version_code,
        )

    for message in read_play_state(play_publisher(), reader):
        print(message)


# --- Play versionCode preflight ----------------------------------------------

UPLOAD_STEP_PREFIX = "Upload to Play"
RELEASE_JOB = "release"


def known_version_codes(bundles: dict, apks: dict, tracks: dict) -> set[int]:
    """Every versionCode Google Play has: uploaded bundles and APKs and track releases."""
    codes: set[int] = set()
    for item in (bundles or {}).get("bundles") or []:
        codes.add(int(item["versionCode"]))
    for item in (apks or {}).get("apks") or []:
        codes.add(int(item["versionCode"]))
    for track in (tracks or {}).get("tracks") or []:
        for release in track.get("releases") or []:
            codes.update(int(code) for code in release.get("versionCodes") or [])
    return codes


def play_upload_decision(
    version_code: int,
    *,
    known_codes: set[int],
    tracks: dict[str, dict],
    selected: list[str],
    earlier_upload_attempt: int | None,
) -> str:
    """'upload' when version_code is new to Google Play; 'skip' when an earlier
    attempt of this same workflow run already uploaded it and every selected track
    carries it as a completed release (a re-run after a later step failed).
    Anything else raises: Play rejects a versionCode it has seen, and an older
    release's versionCode must never be relabelled as this release.
    """
    highest = max(known_codes, default=0)
    if version_code > highest:
        return "upload"
    if version_code in known_codes and earlier_upload_attempt is not None:
        missing = [
            track for track in selected
            if track_release_state(tracks.get(track) or {}, version_code) != "completed"
        ]
        if not missing:
            return "skip"
        raise ReleaseError(
            f"attempt {earlier_upload_attempt} of this run uploaded versionCode {version_code}, but Play "
            f"does not carry it as a completed release on {', '.join(missing)}; finish that release in "
            "Play Console, or bump versionCode and tag again"
        )
    raise ReleaseError(
        f"versionCode {version_code} is not new to Google Play (highest versionCode already uploaded: "
        f"{highest}); bump versionCode in android/app/build.gradle.kts before tagging"
    )


def earlier_upload_attempt(fetch_jobs, run_attempt: int) -> int | None:
    """The latest earlier attempt of this workflow run whose release job finished the
    Play upload step, if any. fetch_jobs(attempt) returns that attempt's jobs."""
    for attempt in range(run_attempt - 1, 0, -1):
        for job in fetch_jobs(attempt):
            if job.get("name") != RELEASE_JOB:
                continue
            for step in job.get("steps") or []:
                if (step.get("name") or "").startswith(UPLOAD_STEP_PREFIX) and step.get("conclusion") == "success":
                    return attempt
    return None


def github_attempt_jobs(attempt: int) -> list[dict[str, Any]]:
    """Jobs of one attempt of the current workflow run (GitHub Actions REST API)."""
    import urllib.request  # noqa: PLC0415

    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    token = os.environ.get("GITHUB_TOKEN", "")
    request = urllib.request.Request(
        f"{api}/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8")).get("jobs") or []


def cmd_play_preflight(args: argparse.Namespace) -> None:
    selected = parse_tracks(args.tracks)
    version_name, version_code = gradle_version(GRADLE.read_text(encoding="utf-8"))
    run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1") or "1")
    earlier = earlier_upload_attempt(github_attempt_jobs, run_attempt) if run_attempt > 1 else None

    def reader(edits, edit_id):
        bundles = edits.bundles().list(packageName=PACKAGE_NAME, editId=edit_id).execute()
        apks = edits.apks().list(packageName=PACKAGE_NAME, editId=edit_id).execute()
        tracks = edits.tracks().list(packageName=PACKAGE_NAME, editId=edit_id).execute()
        return bundles, apks, tracks

    bundles, apks, tracks = read_play_state(play_publisher(), reader)
    known = known_version_codes(bundles, apks, tracks)
    by_track = {track.get("track"): track for track in tracks.get("tracks") or []}
    decision = play_upload_decision(
        version_code,
        known_codes=known,
        tracks=by_track,
        selected=selected,
        earlier_upload_attempt=earlier,
    )
    if decision == "upload":
        print(
            f"versionCode {version_code} ({version_name}) is new to Google Play "
            f"(highest so far: {max(known, default=0)}); it can be uploaded to {', '.join(selected)}"
        )
    else:
        print(
            f"versionCode {version_code} ({version_name}) was uploaded by attempt {earlier} of this run and "
            f"is already completed on {', '.join(selected)}; the Play upload is skipped"
        )
    write_outputs({"play_upload_needed": decision == "upload"})


# --- GitHub Releases versionCode preflight -----------------------------------

RELEASE_VERSION_CODE = re.compile(r"\(versionCode ([0-9]+)\)")


def released_version_codes(releases: list[dict[str, Any]], current_tag: str) -> dict[int, str]:
    """versionCode -> tag for every other android-v* GitHub Release, read from titles
    such as "Android 7.2 (versionCode 42)". Releases without one in the title are skipped."""
    codes: dict[int, str] = {}
    for release in releases:
        tag = release.get("tagName") or ""
        if not tag.startswith(TAG_PREFIX) or tag == current_tag:
            continue
        match = RELEASE_VERSION_CODE.search(release.get("name") or "")
        if match:
            codes.setdefault(int(match.group(1)), tag)
    return codes


def check_new_on_github(version_code: int, codes: dict[int, str]) -> None:
    """The GitHub Release APK must be installable over every earlier one."""
    if not codes:
        return
    highest = max(codes)
    if version_code <= highest:
        raise ReleaseError(
            f"versionCode {version_code} is not higher than versionCode {highest} of the "
            f"{codes[highest]} GitHub Release; bump versionCode in android/app/build.gradle.kts "
            "before tagging (sideloaded installs could not update otherwise)"
        )


def cmd_github_preflight(args: argparse.Namespace) -> None:
    version_name, version_code = gradle_version(GRADLE.read_text(encoding="utf-8"))
    releases = json.loads(args.releases.read_text(encoding="utf-8"))
    codes = released_version_codes(releases, os.environ.get("GITHUB_REF_NAME", ""))
    check_new_on_github(version_code, codes)
    print(
        f"versionCode {version_code} ({version_name}) is higher than every earlier Android "
        f"GitHub Release (highest: {max(codes, default=0)})"
    )


# --- signing properties ------------------------------------------------------


def java_properties_escape(value: str) -> str:
    """Escape a value the way java.util.Properties.store() does, so load() reads it back
    exactly (backslashes, a leading space, control and non-ASCII characters)."""
    out: list[str] = []
    for index, char in enumerate(value):
        code = ord(char)
        if char == " ":
            out.append("\\ " if index == 0 else " ")
        elif char in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[char])
        elif char in "\\=:#!":
            out.append("\\" + char)
        elif code < 0x20 or code > 0x7E:
            units = char.encode("utf-16-be")
            out.extend(f"\\u{units[i] << 8 | units[i + 1]:04X}" for i in range(0, len(units), 2))
        else:
            out.append(char)
    return "".join(out)


SIGNING_SECRETS = (
    ("storePassword", "ANDROID_KEYSTORE_PASSWORD"),
    ("keyAlias", "ANDROID_KEY_ALIAS"),
    ("keyPassword", "ANDROID_KEY_PASSWORD"),
)


def signing_properties(keystore: Path, env: Mapping[str, str]) -> str:
    missing = [name for _, name in SIGNING_SECRETS if not env.get(name)]
    if missing:
        raise ReleaseError(f"missing signing secret(s): {', '.join(missing)}")
    lines = [f"storeFile={java_properties_escape(str(keystore))}"]
    lines += [f"{key}={java_properties_escape(env[name])}" for key, name in SIGNING_SECRETS]
    return "\n".join(lines) + "\n"


def cmd_write_signing_properties(args: argparse.Namespace) -> None:
    text = signing_properties(args.keystore, os.environ)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(text)
    os.chmod(args.out, 0o600)
    print(f"wrote signing properties (mode 600) to {args.out}")


def cmd_gates(_: argparse.Namespace) -> None:
    version_name, version_code = gradle_version(GRADLE.read_text(encoding="utf-8"))
    resolved = resolve_gates(
        os.environ,
        event=os.environ.get("GITHUB_EVENT_NAME", ""),
        ref_name=os.environ.get("GITHUB_REF_NAME", ""),
        version_name=version_name,
    )
    notes = resolved.pop("notes")
    print(f"Android release switches ({version_name}, versionCode {version_code}):")
    for note in notes:
        print(f"  note: {note}")
    if resolved["production"]:
        print("  WARNING: ANDROID_PRODUCTION_ROLLOUT is on — this build goes live on Play production")
    write_outputs(resolved)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gates").set_defaults(func=cmd_gates)
    verify = sub.add_parser("verify")
    verify.add_argument("--aab", required=True, type=Path)
    verify.add_argument("--apk", required=True, type=Path)
    verify.add_argument("--keystore", required=True, type=Path)
    verify.add_argument("--alias", required=True)
    verify.add_argument("--bundletool", required=True, type=Path)
    verify.add_argument("--package", default=PACKAGE_NAME)
    verify.add_argument(
        "--expected-cert", default=UPLOAD_CERT_SHA256, help="pinned upload certificate SHA-256"
    )
    verify.set_defaults(func=cmd_verify)
    tracks = sub.add_parser("check-tracks")
    tracks.add_argument("--tracks", required=True, help="comma-separated Play track ids")
    tracks.set_defaults(func=cmd_check_tracks)
    preflight = sub.add_parser("play-preflight")
    preflight.add_argument("--tracks", required=True, help="comma-separated Play track ids")
    preflight.set_defaults(func=cmd_play_preflight)
    github = sub.add_parser("github-preflight")
    github.add_argument(
        "--releases", required=True, type=Path,
        help="JSON from `gh release list --json tagName,name`",
    )
    github.set_defaults(func=cmd_github_preflight)
    signing = sub.add_parser("write-signing-properties")
    signing.add_argument("--keystore", required=True, type=Path)
    signing.add_argument("--out", required=True, type=Path)
    signing.set_defaults(func=cmd_write_signing_properties)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
