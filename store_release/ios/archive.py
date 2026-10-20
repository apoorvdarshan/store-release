"""The iOS build job's local steps: select Xcode, the SPM cache key, archive checks, export options.

Stdlib only: the macOS build job never installs the store lock.

  select-xcode     Refuse a beta Xcode, export DEVELOPER_DIR, print `xcodebuild -version`
                   and write the step output spm_key (Xcode version and build, a digest
                   of developer_dir and of the package_cache_files).
  verify-archive   The archive's main app is this bundle id, and every app and extension
                   bundle in it (frameworks excluded) carries this version and build;
                   then the archived app's export compliance.
  export-options   The ExportOptions.plist for a validation (export) or release (upload) run.
  verify-bundles   With ios.build.extensions / app_groups: exactly one app and exactly the configured
                   extensions, each with its bundle id, version, build, team and app groups in its
                   signature; --kind distribution (an exported IPA's Payload) also checks the strict
                   signature, an Apple Distribution certificate, an App Store profile and no
                   get-task-allow.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

from store_release.config import STANDARD_FLOW
from store_release.ios import release as ios_release

XCODE_VERSION = re.compile(r"^Xcode ([0-9][0-9.]*)\s*$", re.MULTILINE)
XCODE_BUILD = re.compile(r"^Build version ([0-9A-Za-z]+)\s*$", re.MULTILINE)


class ArchiveError(Exception):
    """An archive, toolchain or export-options check failed."""


def xcode_identity(version_output: str) -> str:
    """xcode<version>-<build> from `xcodebuild -version` (e.g. xcode27.0-27A100)."""
    version = XCODE_VERSION.search(version_output)
    build = XCODE_BUILD.search(version_output)
    if not version or not build:
        raise ArchiveError(f"cannot read the Xcode version and build from: {version_output.strip()!r}")
    return f"xcode{version.group(1)}-{build.group(1)}"


def package_files(app_root: Path, patterns: Iterable[str]) -> list[str]:
    """Repository-relative files matched by the package_cache_files globs, sorted, inside app_root."""
    root = app_root.resolve()
    found: set[str] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                resolved = path.resolve()
                if root in resolved.parents:
                    found.add(path.relative_to(root).as_posix())
    return sorted(found)


def spm_digest(app_root: Path, developer_dir: str, patterns: Iterable[str]) -> str:
    """40 hex of sha256 over sha256(developer_dir), then `relpath\\0sha256(content)\\n` per file."""
    digest = hashlib.sha256(hashlib.sha256(developer_dir.encode("utf-8")).digest())
    for relative in package_files(app_root, patterns):
        content = hashlib.sha256((app_root / relative).read_bytes()).hexdigest()
        digest.update(f"{relative}\0{content}\n".encode("utf-8"))
    return digest.hexdigest()[:40]


def spm_key(*, runner_os: str, runner_arch: str, xcode_id: str, digest: str) -> str:
    return f"ios-release-spm-{runner_os}-{runner_arch}-{xcode_id}-{digest}"


def select_xcode(
    app_root: Path,
    developer_dir: str,
    patterns: Iterable[str],
    *,
    environ: dict[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Check and select the configured Xcode; returns the SPM cache key."""
    env = dict(os.environ if environ is None else environ)
    if "beta" in developer_dir.lower():
        raise ArchiveError(f"App Store builds need a released Xcode, not {developer_dir}")
    if not Path(developer_dir).is_dir():
        raise ArchiveError(f"ios.build.developer_dir {developer_dir} does not exist on this runner")
    result = run(["xcodebuild", "-version"], capture_output=True, text=True,
                 env={**env, "DEVELOPER_DIR": developer_dir}, check=False)
    if result.returncode != 0:
        raise ArchiveError(f"xcodebuild -version failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
    print(result.stdout.strip())
    xcode_id = xcode_identity(result.stdout)
    key = spm_key(
        runner_os=env.get("RUNNER_OS", "unknown"),
        runner_arch=env.get("RUNNER_ARCH", "unknown"),
        xcode_id=xcode_id,
        digest=spm_digest(app_root, developer_dir, patterns),
    )
    github_env = env.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"DEVELOPER_DIR={developer_dir}\n")
    github_output = env.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"spm_key={key}\n")
    print(f"selected {developer_dir} ({xcode_id}); Swift package cache key {key}")
    return key


def read_plist(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ArchiveError(f"cannot read {path}: {exc}") from None


def archive_bundles(main: Path) -> list[Path]:
    """The main app, then every *.app / *.appex directory under it outside Frameworks/."""
    bundles = [main]
    for path in sorted(main.rglob("*")):
        if not path.is_dir() or not path.name.endswith((".app", ".appex")):
            continue
        if "Frameworks" in path.relative_to(main).parts:
            continue
        bundles.append(path)
    return bundles


def plist_text(value: object) -> str | None:
    """A version value as fud-ai's PlistBuddy check compared it: a string as is, an integer in
    decimal (PlistBuddy prints both the same way); None for a missing key or any other type."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def shown_value(value: object) -> str:
    if value is None:
        return "missing"
    if isinstance(value, str):
        return value
    return f"{value!r} (a {type(value).__name__}, not a string)"


def verify_archive(archive: Path, *, bundle_id: str, version: str, build: str) -> tuple[str, int]:
    """Check the archive; returns (main app Info.plist path, bundles checked)."""
    apps = archive / "Products" / "Applications"
    candidates = sorted(apps.glob("*.app")) if apps.is_dir() else []
    if not candidates:
        raise ArchiveError("the archive holds no .app under Products/Applications")
    main = candidates[0]
    found = read_plist(main / "Info.plist").get("CFBundleIdentifier")
    if found != bundle_id:
        raise ArchiveError(f"archived {found}, expected {bundle_id}")
    checked = 0
    for bundle in archive_bundles(main):
        plist = read_plist(bundle / "Info.plist")
        short = plist.get("CFBundleShortVersionString")
        number = plist.get("CFBundleVersion")
        if plist_text(short) != version or plist_text(number) != build:
            shown = bundle.relative_to(apps).as_posix()
            raise ArchiveError(f"{shown} is {shown_value(short)} ({shown_value(number)}), "
                               f"expected {version} ({build})")
        checked += 1
    return str(main / "Info.plist"), checked


Runner = Callable[..., subprocess.CompletedProcess]


def _plist_bytes(raw: bytes, what: str) -> dict:
    try:
        data = plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise ArchiveError(f"{what} is not a readable plist ({exc})") from None
    if not isinstance(data, dict):
        raise ArchiveError(f"{what} is not a dictionary")
    return data


def bundle_entitlements(bundle: Path, run: Runner) -> dict:
    result = run(["codesign", "-d", "--entitlements", "-", "--xml", str(bundle)], capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout:
        raise ArchiveError(f"{bundle.name} has no readable code signature")
    return _plist_bytes(result.stdout, f"{bundle.name}'s entitlements")


def verify_bundles(apps_dir: Path, *, kind: str, bundle_id: str, extensions: Iterable[str],
                   app_groups: dict[str, tuple[str, ...]], team_id: str, version: str, build: str,
                   run: Runner = subprocess.run) -> list[str]:
    """The app and its extensions in the archive (kind "archive") or the App Store-signed IPA's
    Payload (kind "distribution"). Returns one line per bundle checked."""
    if kind not in ("archive", "distribution"):
        raise ArchiveError(f"unknown kind {kind}")
    apps = sorted(apps_dir.glob("*.app")) if apps_dir.is_dir() else []
    if len(apps) != 1:
        raise ArchiveError(f"expected one app in {apps_dir}, found {len(apps)}")
    main = apps[0]
    found = [b for b in archive_bundles(main)[1:] if b.name.endswith(".appex")]
    wanted = sorted(extensions)
    ids = {}
    for bundle in found:
        ids[bundle] = read_plist(bundle / "Info.plist").get("CFBundleIdentifier")
    if sorted(str(v) for v in ids.values()) != wanted:
        shown = ", ".join(f"{b.relative_to(main).as_posix()} ({i})" for b, i in ids.items()) or "none"
        raise ArchiveError(f"expected only the extensions {', '.join(wanted) or 'none'}, found: {shown}")
    lines = []
    for bundle, expected in [(main, bundle_id), *((b, str(i)) for b, i in ids.items())]:
        info = read_plist(bundle / "Info.plist")
        if info.get("CFBundleIdentifier") != expected:
            raise ArchiveError(f"{bundle.name} is {info.get('CFBundleIdentifier')}, expected {expected}")
        short, number = plist_text(info.get("CFBundleShortVersionString")), plist_text(info.get("CFBundleVersion"))
        if short != version or number != build:
            raise ArchiveError(f"{bundle.name} is {short} ({number}), expected {version} ({build})")
        entitlements = bundle_entitlements(bundle, run)
        if entitlements.get("application-identifier") != f"{team_id}.{expected}":
            raise ArchiveError(f"{bundle.name} application-identifier is {entitlements.get('application-identifier')}")
        if entitlements.get("com.apple.developer.team-identifier") != team_id:
            raise ArchiveError(f"{bundle.name} is not signed for team {team_id}")
        groups = app_groups.get(expected, ())
        held = entitlements.get("com.apple.security.application-groups") or []
        for group in groups:
            if group not in held:
                raise ArchiveError(f"{bundle.name} lost the {group} app group entitlement")
        if kind == "distribution":
            if run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=False).returncode != 0:
                raise ArchiveError(f"{bundle.name} signature does not verify")
            details = run(["codesign", "-dvv", str(bundle)], capture_output=True, text=True, check=False)
            authority = [line for line in (details.stderr or "").splitlines() if line.startswith("Authority=")]
            if not authority or not re.match(r"^Authority=(Apple|iPhone) Distribution", authority[0]):
                raise ArchiveError(f"{bundle.name} is not signed for distribution: {authority[0] if authority else 'none'}")
            if entitlements.get("get-task-allow") is True:
                raise ArchiveError(f"{bundle.name} allows debugging (get-task-allow)")
            profile = run(["security", "cms", "-D", "-i", str(bundle / "embedded.mobileprovision")],
                          capture_output=True, check=False)
            if profile.returncode != 0 or not profile.stdout:
                raise ArchiveError(f"{bundle.name} has no readable embedded.mobileprovision")
            prov = _plist_bytes(profile.stdout, f"{bundle.name}'s provisioning profile")
            if prov.get("ProvisionedDevices"):
                raise ArchiveError(f"{bundle.name} has a development or ad hoc profile")
            prov_entitlements = prov.get("Entitlements") or {}
            if prov_entitlements.get("application-identifier") != f"{team_id}.{expected}":
                raise ArchiveError(f"{bundle.name} provisioning profile is for "
                                   f"{prov_entitlements.get('application-identifier')}")
            for group in groups:
                if group not in (prov_entitlements.get("com.apple.security.application-groups") or []):
                    raise ArchiveError(f"{bundle.name} provisioning profile does not grant {group}")
        shown_groups = f", app groups {', '.join(groups)}" if groups else ""
        lines.append(f"  {bundle.name}: {expected} {short} ({number}){shown_groups}")
    return lines


def export_options(*, mode: str, team_id: str) -> bytes:
    if mode not in ("release", "validate"):
        raise ArchiveError(f"mode must be release or validate, got {mode!r}")
    options = {
        "method": "app-store-connect",
        "teamID": team_id,
        "signingStyle": "automatic",
        "destination": "upload" if mode == "release" else "export",
        "manageAppVersionAndBuildNumber": False,
        "uploadSymbols": True,
    }
    return plistlib.dumps(options, fmt=plistlib.FMT_XML)


def archived_export_compliance(info_plist: Path, config=None) -> None:
    """The archived app's export compliance answer, with the check the app's flow uses."""
    if config is not None and config.ios is not None and config.ios.flow != STANDARD_FLOW:
        from store_release.flows import commands  # noqa: PLC0415

        commands.export_compliance(config.ios.flow, commands.module(config.ios.flow, "ios_release"),
                                   ["--info-plist", str(info_plist)])
        return
    answer = ios_release.check_archived_export_compliance(info_plist)
    detail = "no non-exempt encryption" if answer == "NO" else "non-exempt encryption with a compliance code"
    print(f"export compliance: {ios_release.ENCRYPTION_KEY} = {answer} ({detail}) in {info_plist}")


def main(argv: list[str], *, app_root: Path, developer_dir: str, patterns: Iterable[str],
         bundle_id: str, team_id: str, config=None) -> None:
    parser = argparse.ArgumentParser(prog="ios", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("select-xcode")
    verify = sub.add_parser("verify-archive")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--version", required=True)
    verify.add_argument("--build", required=True)
    options = sub.add_parser("export-options")
    options.add_argument("--mode", required=True, choices=("release", "validate"))
    options.add_argument("--out", required=True, type=Path)
    bundles = sub.add_parser("verify-bundles")
    bundles.add_argument("--apps", required=True, type=Path)
    bundles.add_argument("--kind", required=True, choices=("archive", "distribution"))
    bundles.add_argument("--version", required=True)
    bundles.add_argument("--build", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "select-xcode":
            select_xcode(app_root, developer_dir, patterns)
        elif args.command == "verify-archive":
            info_plist, checked = verify_archive(args.archive, bundle_id=bundle_id,
                                                 version=args.version, build=args.build)
            print(f"verified {bundle_id} {args.version} ({args.build}) in {checked} app/extension bundles")
            archived_export_compliance(Path(info_plist), config)
        elif args.command == "verify-bundles":
            build = config.ios.build if config is not None else None
            extensions = build.extensions if build else ()
            groups = dict(build.app_groups) if build else {}
            if not (extensions or groups):
                print("no ios.build.extensions or app_groups: nothing more to verify")
                return
            for line in verify_bundles(args.apps, kind=args.kind, bundle_id=bundle_id, extensions=extensions,
                                       app_groups=groups, team_id=team_id, version=args.version, build=args.build):
                print(line)
            print(f"verified the {args.kind} app and extensions for {args.version} ({args.build})")
        else:
            args.out.write_bytes(export_options(mode=args.mode, team_id=team_id))
            print(f"wrote {args.out} ({args.mode})")
    except (ArchiveError, ios_release.ReleaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
