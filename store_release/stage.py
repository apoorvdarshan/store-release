"""Job-boundary checks: the tooling identity, config export, stage inputs, secret presence.

Jobs pass each other only fixed-format scalars. Every stage re-validates its
inputs and fails closed when one is empty or malformed, which is how an output
GitHub dropped (an output that contains a masked secret is not passed on)
shows up as a clear error instead of a silently skipped step.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

from store_release import __version__
from store_release import config as config_mod
from store_release.config import Config

SHA40 = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+(\.[0-9]+){0,2}")
DIGITS = re.compile(r"[0-9]+")
EPOCH = re.compile(r"[0-9]{9,11}")
BUILD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RESULT = r"(success|failure|cancelled|skipped)"
GATES = re.compile(rf"[a-z0-9][a-z0-9_-]*={RESULT}( [a-z0-9][a-z0-9_-]*={RESULT})*")
# The jobs that start a run and so have no earlier tool_sha to compare with.
FIRST_JOBS = {"ios-prepare", "android-gates", "checks"}
STAGES = ("ios-build", "ios-wait-for-build", "ios-app-store-connect", "ios-github-release", "ios-source-archive",
          "ios-store-plan", "android-publish", "android-store-plan")


class StageError(Exception):
    """A job input, the tooling identity or a gate result is not what this stage needs."""


# --- tool check ------------------------------------------------------------------


def tool_check(tool_root: Path, tool_sha: str, expected: str, *, job: str = "",
               head: str | None = None) -> str:
    if not SHA40.fullmatch(tool_sha or ""):
        raise StageError(f"--tool-sha {tool_sha!r} is not a 40-hex commit SHA (job.workflow_sha)")
    if head is None:
        result = subprocess.run(["git", "-C", str(tool_root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise StageError(f"cannot read the store-release checkout's commit: {result.stderr.strip()}")
        head = result.stdout.strip()
    if head != tool_sha:
        raise StageError(f"the store-release checkout is at {head}, not this workflow's commit {tool_sha}")
    if expected:
        if not SHA40.fullmatch(expected):
            raise StageError(f"--expected {expected!r} is not a 40-hex commit SHA")
        if expected != tool_sha:
            raise StageError(
                f"this job runs store-release {tool_sha}, but this run's first job ran {expected}; "
                "every job of a run must use the same store-release commit"
            )
    elif job and job not in FIRST_JOBS:
        raise StageError(f"{job} got no tool_sha from this run's prepare/gates job (was the output dropped?)")
    message = f"store-release v{__version__} ({tool_sha})"
    print(message)
    return message


# --- config export -----------------------------------------------------------------

SAFE_PATH = re.compile(r"[A-Za-z0-9._/+@ -]+")
APP_NAME = re.compile(r"[^\n\r\x00]{1,64}")
TASKS = re.compile(r":?[A-Za-z0-9_.:-]+( :?[A-Za-z0-9_.:-]+)*")
BOOLEAN = re.compile(r"true|false")
FLOW = re.compile(r"[a-z][a-z-]*")
WORD = re.compile(r"[A-Za-z0-9_.-]+")
SDK_PACKAGES = re.compile(r"([A-Za-z0-9_.;-]+( [A-Za-z0-9_.;-]+)*)?")


def export_values(config: Config, job: str, config_path: str = config_mod.DEFAULT_CONFIG
                  ) -> list[tuple[str, str, re.Pattern]]:
    values = [
        ("STORE_RELEASE_CONFIG", config_path, SAFE_PATH),
        ("STORE_RELEASE_SLUG", config.app.slug, config_mod.SLUG),
        ("STORE_RELEASE_APP_NAME", config.app.name, APP_NAME),
    ]
    want_ios = job.startswith("ios-") or job == "checks"
    want_android = job.startswith("android-") or job == "checks"
    if job.startswith("ios-") and config.ios is None:
        raise StageError(f"{job} needs an [ios] table in store-release.toml")
    if job.startswith("android-") and config.android is None:
        raise StageError(f"{job} needs an [android] table in store-release.toml")
    if job == "checks":
        # The store checks skip the steps of a platform the app does not have.
        values += [
            ("STORE_RELEASE_HAS_IOS", "true" if config.ios is not None else "false", BOOLEAN),
            ("STORE_RELEASE_HAS_ANDROID", "true" if config.android is not None else "false", BOOLEAN),
            ("STORE_RELEASE_PORTED_FLOW", "true" if config.ported_flow else "false", BOOLEAN),
            ("STORE_RELEASE_UNIQUE_HEADINGS", "true" if config.unique_headings else "false", BOOLEAN),
        ]
    if want_ios and config.ios is not None:
        ios = config.ios
        values += [
            ("STORE_RELEASE_IOS_TAG_PREFIX", ios.tag_prefix, config_mod.TAG_PREFIX),
            ("STORE_RELEASE_IOS_BUNDLE_ID", ios.bundle_id, config_mod.BUNDLE_ID),
            ("STORE_RELEASE_IOS_TEAM_ID", ios.team_id, config_mod.TEAM_ID),
            ("STORE_RELEASE_IOS_PROJECT", ios.project, SAFE_PATH),
            ("STORE_RELEASE_IOS_SCHEME", ios.scheme, config_mod.SCHEME),
            ("STORE_RELEASE_IOS_LOCALE", ios.locale, config_mod.LOCALE),
            ("STORE_RELEASE_IOS_FLOW", ios.flow, FLOW),
            ("STORE_RELEASE_IOS_VERIFY_IPA", "true" if ios.build.verify_ipa else "false", BOOLEAN),
            ("STORE_RELEASE_IOS_SOURCE_ARCHIVE", "true" if ios.source_archive else "false", BOOLEAN),
        ]
    if want_android and config.android is not None:
        android = config.android
        values += [
            ("STORE_RELEASE_ANDROID_TAG_PREFIX", android.tag_prefix, config_mod.TAG_PREFIX),
            ("STORE_RELEASE_ANDROID_PACKAGE", android.package, config_mod.PACKAGE),
            ("STORE_RELEASE_ANDROID_LOCALE", android.locale, config_mod.LOCALE),
            ("STORE_RELEASE_ANDROID_BUILD_DIR", android.build.dir, SAFE_PATH),
            ("STORE_RELEASE_ANDROID_TASKS", " ".join(android.build.tasks), TASKS),
            ("STORE_RELEASE_ANDROID_AAB", android.build.aab, SAFE_PATH),
            ("STORE_RELEASE_ANDROID_APK", android.build.apk, SAFE_PATH),
            ("STORE_RELEASE_ANDROID_KEYSTORE_PROPERTIES", android.build.keystore_properties, SAFE_PATH),
            ("STORE_RELEASE_ANDROID_GRADLE_FILE", android.gradle_file, SAFE_PATH),
            ("STORE_RELEASE_ANDROID_FLOW", android.flow, FLOW),
            ("STORE_RELEASE_ANDROID_SIGNING", android.build.signing, WORD),
            ("STORE_RELEASE_ANDROID_SDK_PACKAGES", " ".join(android.build.sdk_packages), SDK_PACKAGES),
        ]
        if android.build.signing == "gradle-properties":
            values += [
                ("STORE_RELEASE_ANDROID_KEYSTORE_PROPERTY", android.build.keystore_property, WORD),
                ("STORE_RELEASE_ANDROID_PASSWORD_FILE_PROPERTY", android.build.password_file_property, WORD),
                ("STORE_RELEASE_ANDROID_KEY_ALIAS", android.build.key_alias, WORD),
            ]
    return values


def config_export(config: Config, job: str, *, environ: Mapping[str, str] | None = None,
                  config_path: str = config_mod.DEFAULT_CONFIG) -> list[str]:
    env = os.environ if environ is None else environ
    lines = []
    for name, value, pattern in export_values(config, job, config_path):
        if "\n" in value or "\r" in value or not pattern.fullmatch(value) or (value == "" and pattern is not SDK_PACKAGES):
            raise StageError(f"{name}: {value!r} is not safe to export")
        lines.append(f"{name}={value}")
    target = env.get("GITHUB_ENV")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    for line in lines:
        print(f"  {line}")
    return lines


# --- secrets check -----------------------------------------------------------------


def secrets_check(names: list[str], *, environ: Mapping[str, str] | None = None) -> None:
    """Fail unless HAS_<NAME> is "true" for every name. Never reads a secret value."""
    env = os.environ if environ is None else environ
    missing, unmapped = [], []
    for name in names:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise StageError(f"{name!r} is not a secret name")
        flag = env.get(f"HAS_{name}")
        if flag is None:
            unmapped.append(name)
        elif flag != "true":
            missing.append(name)
    if unmapped:
        raise StageError(f"the workflow sets no HAS_ presence flag for: {', '.join(unmapped)}")
    if missing:
        raise StageError(
            f"missing secret(s): {', '.join(missing)}; set them in the app repository's "
            "Settings → Secrets and variables → Actions, and map them in the caller workflow"
        )
    print(f"secrets present: {', '.join(names)}")


# --- stage check -------------------------------------------------------------------


def parse_gates(raw: str) -> dict[str, str]:
    if not GATES.fullmatch(raw or ""):
        raise StageError(f"--gates {raw!r} is not a list of <job>=<result> (was a job output dropped?)")
    results: dict[str, str] = {}
    for item in raw.split(" "):
        name, result = item.split("=", 1)
        if name in results:
            raise StageError(f"--gates lists {name} twice")
        results[name] = result
    return results


def gate_check(raw: str, configured: list[str], *, release: bool, skipped_ok: bool = False) -> dict[str, str]:
    """The gate jobs' results this stage may run after (the caller's `if:`, checked again).

    skipped_ok: ios.gate_policy = "when-uploading", on a tag run that uploads no build: the
    gate jobs did not run (PCM Notes' caller), so all of them must be skipped.
    """
    results = parse_gates(raw)
    if sorted(results) != sorted(configured) or len(results) != len(configured):
        raise StageError(f"--gates names {', '.join(results) or 'nothing'}, but the config's gate jobs are "
                         f"{', '.join(configured)}")
    values = set(results.values())
    if skipped_ok:
        if values == {"skipped"}:
            return results
        table = "\n  ".join(f"{name}: {result}" for name, result in results.items())
        raise StageError(f"with ios.gate_policy = when-uploading, a run that uploads no build never runs the gate "
                         f"jobs; got:\n  {table}")
    if release and values == {"success"}:
        return results
    if not release and values in ({"success"}, {"skipped"}):
        return results
    table = "\n  ".join(f"{name}: {result}" for name, result in results.items())
    need = "every gate job to succeed" if release else "every gate job to succeed, or all to be skipped"
    raise StageError(f"this stage needs {need}; got:\n  {table}")


def boolean(name: str, raw: str) -> bool:
    if raw not in ("true", "false"):
        raise StageError(f"--{name} {raw!r} must be exactly true or false (was a job output dropped?)")
    return raw == "true"


def matches(pattern: re.Pattern, name: str, raw: str, *, allow_empty: bool = False) -> str:
    if allow_empty and raw == "":
        return raw
    if not pattern.fullmatch(raw or ""):
        raise StageError(f"--{name} {raw!r} is not valid (was a job output dropped?)")
    return raw


def bounded(name: str, raw: str, low: int, high: int) -> int:
    matches(DIGITS, name, raw)
    if not low <= int(raw) <= high:
        raise StageError(f"--{name} {raw} must be between {low} and {high}")
    return int(raw)


def event_mode(env: Mapping[str, str]) -> str:
    event = env.get("GITHUB_EVENT_NAME", "")
    if event == "push":
        return "release"
    if event == "workflow_dispatch":
        return "validate"
    raise StageError(f"release workflows run on a tag push or a manual dispatch, not {event or 'an unknown event'}")


def require_push(env: Mapping[str, str], stage: str) -> None:
    if event_mode(env) != "release":
        raise StageError(f"{stage} runs only on a tag push")


def require_tag(env: Mapping[str, str], prefix: str, version: str) -> None:
    ref = env.get("GITHUB_REF_NAME", "")
    if ref != prefix + version:
        raise StageError(f"this run is for {ref or 'no ref'}, but its version input is {version} ({prefix}{version})")


def stage_check(stage: str, args: argparse.Namespace, config: Config, *,
                environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    if stage.startswith("ios-"):
        if config.ios is None:
            raise StageError(f"{stage} needs an [ios] table in store-release.toml")
        _ios_stage(stage, args, config, env)
    elif stage.startswith("android-"):
        if config.android is None:
            raise StageError(f"{stage} needs an [android] table in store-release.toml")
        _android_stage(stage, args, config, env)
    else:
        raise StageError(f"unknown stage {stage!r}")
    print(f"{stage}: inputs checked")


def _project_version(config: Config | None = None) -> str:
    if config is not None and config.ios is not None and config.ios.flow != "fud-ai":
        from store_release.flows import commands  # noqa: PLC0415

        release = commands.module(config.ios.flow, "ios_release")
        try:
            return commands.project_value(config.ios.flow, "MARKETING_VERSION")
        except release.ReleaseError as exc:
            raise StageError(str(exc)) from None
    from store_release.ios import release as ios_release  # noqa: PLC0415

    try:
        return ios_release.single_project_value("MARKETING_VERSION")
    except ios_release.ReleaseError as exc:
        raise StageError(str(exc)) from None


def _ios_stage(stage: str, args: argparse.Namespace, config: Config, env: Mapping[str, str]) -> None:
    ios = config.ios
    gate_ids = [job.id for job in ios.gate_jobs]
    prefix = ios.tag_prefix
    if stage == "ios-build":
        mode = event_mode(env)
        if args.mode != mode:
            raise StageError(f"--mode {args.mode!r} does not match this {env.get('GITHUB_EVENT_NAME')} run ({mode})")
        version = matches(VERSION, "marketing-version", args.marketing_version)
        if mode == "release":
            require_tag(env, prefix, version)
            project = _project_version(config)
            if version != project:
                raise StageError(f"--marketing-version {version} is not the project's MARKETING_VERSION {project}")
        else:
            wanted = args.dispatch_version.strip() if args.dispatch_version else ""
            expected = wanted or _project_version(config)
            if version != expected:
                raise StageError(f"--marketing-version {version} is not the version this dispatch validates ({expected})")
        matches(DIGITS, "build-number", args.build_number)
        gate_check(args.gates, gate_ids, release=mode == "release")
    elif stage == "ios-wait-for-build":
        require_push(env, stage)
        version = matches(VERSION, "marketing-version", args.marketing_version)
        require_tag(env, prefix, version)
        matches(DIGITS, "build-number", args.build_number)
        matches(EPOCH, "upload-started-at", args.upload_started_at)
        bounded("wait-minutes", args.wait_minutes, 10, 300)
        bounded("poll-seconds", args.poll_seconds, 10, 600)
    elif stage == "ios-app-store-connect":
        require_push(env, stage)
        version = matches(VERSION, "marketing-version", args.marketing_version)
        require_tag(env, prefix, version)
        flags = {name: boolean(name, getattr(args, name.replace("-", "_")))
                 for name in ("upload-build", "listing", "whats-new", "screenshots", "creative", "submit",
                              "public-release")}
        if not any(flags[name] for name in ("listing", "whats-new", "screenshots", "creative", "submit")):
            raise StageError("app-store-connect runs only with a listing, What's New, screenshot, creative "
                             "or review switch on")
        if flags["submit"] and not flags["upload-build"]:
            raise StageError("--submit needs --upload-build: review submission only uses this run's build")
        if flags["public-release"] and not flags["submit"]:
            raise StageError("--public-release needs --submit")
        if flags["upload-build"]:
            matches(BUILD_ID, "build-id", args.build_id)
            matches(DIGITS, "build-number", args.build_number)
        elif args.build_id:
            raise StageError(f"--build-id {args.build_id!r} given, but this run uploads no build")
        gate_check(args.gates, gate_ids, release=True,
                   skipped_ok=ios.gate_policy == "when-uploading" and not flags["upload-build"])
    elif stage == "ios-source-archive":
        if not ios.source_archive:
            raise StageError("ios-source-archive runs only with ios.github_release.source_archive = true")
        require_push(env, stage)
        version = matches(VERSION, "marketing-version", args.marketing_version)
        require_tag(env, prefix, version)
        matches(DIGITS, "uploaded-build", args.uploaded_build, allow_empty=True)
        gate_check(args.gates, gate_ids, release=True,
                   skipped_ok=ios.gate_policy == "when-uploading" and args.uploaded_build == "")
    elif stage == "ios-github-release":
        require_push(env, stage)
        version = matches(VERSION, "marketing-version", args.marketing_version)
        require_tag(env, prefix, version)
        matches(DIGITS, "uploaded-build", args.uploaded_build, allow_empty=True)
        gate_check(args.gates, gate_ids, release=True,
                   skipped_ok=ios.gate_policy == "when-uploading" and args.uploaded_build == "")
    elif stage == "ios-store-plan":
        if event_mode(env) != "validate":
            raise StageError("store-plan runs only on a manual validation dispatch")
        version = matches(VERSION, "marketing-version", args.marketing_version)
        wanted = args.dispatch_version.strip() if args.dispatch_version else ""
        expected = wanted or _project_version(config)
        if version != expected:
            raise StageError(f"--marketing-version {version} is not the version this dispatch validates ({expected})")
    else:
        raise StageError(f"unknown stage {stage!r}")


def _android_stage(stage: str, args: argparse.Namespace, config: Config, env: Mapping[str, str]) -> None:
    from store_release.android import release as android_release  # noqa: PLC0415

    android = config.android
    if stage == "android-store-plan":
        if event_mode(env) != "validate":
            raise StageError("store-plan runs only on a manual validation dispatch")
        return
    if stage != "android-publish":
        raise StageError(f"unknown stage {stage!r}")
    mode = event_mode(env)
    if args.mode != mode:
        raise StageError(f"--mode {args.mode!r} does not match this {env.get('GITHUB_EVENT_NAME')} run ({mode})")
    flags = {name: boolean(name, getattr(args, name.replace("-", "_")))
             for name in ("build", "github-release", "listing", "whats-new", "screenshots", "production",
                          "play-upload")}
    if not flags["build"]:
        raise StageError("the release job runs only when gates resolved build=true")
    tracks_raw = args.play_tracks or ""
    if mode == "release":
        try:
            version_name, _ = android_release.gradle_version(android_release.GRADLE.read_text(encoding="utf-8"))
        except android_release.ReleaseError as exc:
            raise StageError(str(exc)) from None
        require_tag(env, android.tag_prefix, version_name)
        if not flags["github-release"]:
            raise StageError("a release run with build=true must create the GitHub Release (github_release=true)")
    else:
        on = [name for name in ("github-release", "listing", "whats-new", "screenshots", "production",
                                "play-upload") if flags[name]]
        if on or tracks_raw:
            raise StageError(f"a validation run must have every store operation off; got {', '.join(on) or tracks_raw}")
    if flags["play-upload"] != (tracks_raw != ""):
        raise StageError(f"--play-upload {flags['play-upload']} does not match --play-tracks {tracks_raw!r}")
    tracks: list[str] = []
    if tracks_raw:
        try:
            tracks = android_release.parse_tracks(tracks_raw)
        except android_release.ReleaseError as exc:
            raise StageError(str(exc)) from None
    if flags["production"] != ("production" in tracks):
        raise StageError(f"--production {flags['production']} does not match the Play tracks {tracks_raw!r}")
    if flags["whats-new"] and not flags["play-upload"]:
        raise StageError("--whats-new needs a Play upload (a track)")
    gate_check(args.gates, [job.id for job in android.gate_jobs], release=mode == "release")


def add_stage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stage", required=True, choices=STAGES)
    for name in ("gates", "mode", "marketing-version", "build-number", "dispatch-version", "upload-started-at",
                 "wait-minutes", "poll-seconds", "build-id", "upload-build", "listing", "whats-new", "screenshots",
                 "creative", "submit", "public-release", "uploaded-build", "build", "github-release", "production",
                 "play-upload", "play-tracks"):
        parser.add_argument(f"--{name}", default="")
