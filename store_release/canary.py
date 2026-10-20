"""store-release CI only: the store checks against a real app's main branch (advisory).

The checks.yml sequence without the pins, caller, yanked and artifact steps
(those compare an app's pinned version, which a canary of this commit is not),
plus the expectations in tests/canary/<app>.toml: the creative images' names
and byte sizes (the Asset Library matches by name and size) and the staged
screenshot sets. Every step runs; the canary fails if any failed. It never
uploads anything.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
RUN_PY = TOOL_ROOT / "run.py"


def png_header(path: Path) -> tuple[int, int, int]:
    raw = path.read_bytes()[:26]
    width, height = struct.unpack(">II", raw[16:24])
    return width, height, raw[25]


class Canary:
    def __init__(self, app_root: Path, work: Path) -> None:
        self.app_root = app_root
        self.work = work
        self.failures: list[str] = []

    def step(self, name: str, *args: str, env: dict[str, str] | None = None) -> bool:
        print(f"::group::{name}", flush=True)
        environ = {k: v for k, v in os.environ.items()
                   if not k.startswith(("IOS_", "ANDROID_", "UPLOAD_", "SUBMIT_", "PUBLIC_", "STORE_"))}
        environ.update(env or {})
        result = subprocess.run([sys.executable, "-I", "-u", str(RUN_PY), "--app-root", str(self.app_root), *args],
                                env=environ, check=False)
        print("::endgroup::", flush=True)
        if result.returncode != 0:
            self.failures.append(f"{name} (exit {result.returncode})")
            print(f"::error title=canary::{name} failed (exit {result.returncode})", flush=True)
        return result.returncode == 0

    def expect(self, condition: bool, message: str) -> None:
        if condition:
            print(f"ok: {message}")
        else:
            self.failures.append(message)
            print(f"::error title=canary::{message}")


def run(app_root: Path, expectations: dict[str, Any], work: Path) -> int:
    canary = Canary(app_root, work)
    meta = work / "store-meta"
    canary.step("Print gates", "print-switches", "--platform", "all")
    canary.step("Test the store release tooling (stdlib)", "selftest", "--platform", "all")
    canary.step("Check every file the config names", "config", "check", "--files")
    canary.step("Validate the catalog", "catalog", "check")
    canary.step("Prepare What's New", "whats-new", "prepare", "--platform", "all", "--tag", "dry-run",
                "--out", str(work / "whatsnew"))
    canary.step("Prepare listing text", "listing", "prepare", "--platform", "all", "--out", str(work / "listing"))
    canary.step("Prepare the metadata layout (listing)", "listing", "prepare", "--platform", "all", "--out", str(meta))
    canary.step("Prepare the metadata layout (What's New)", "whats-new", "prepare", "--platform", "all", "--tag",
                "dry-run", "--out", str(meta))
    canary.step("Stage screenshots (dry run)", "screenshots", "stage", "--dry-run", "--out", str(work / "dry-staged"))
    canary.step("Validate the ASC store step (dry run)", "ios", "store-step", "--dry-run", "--metadata-dir", str(meta),
                "--build-id", "DRY-RUN", "--build-number", "1",
                env={"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "SUBMIT_IOS_REVIEW": "true",
                     "PUBLIC_RELEASE": "false"})
    canary.step("Validate the creative images (dry run)", "ios", "store-step", "--dry-run",
                env={"UPLOAD_CREATIVE_ASSETS": "true"})
    canary.step("Validate the Play listing (dry run)", "android", "play-listing", "--dry-run", "--metadata-dir",
                str(meta), env={"UPLOAD_LISTING": "true"})
    canary.step("RevenueCat catalog dry run", "revenuecat-gate", env={"STORE_SYNC_REVENUECAT": "false"})
    canary.step("Project MARKETING_VERSION", "ios", "project-version", "--setting", "MARKETING_VERSION")
    canary.step("Project CURRENT_PROJECT_VERSION", "ios", "project-version", "--setting", "CURRENT_PROJECT_VERSION")
    canary.step("Project export compliance", "ios", "export-compliance", "--project")
    canary.step("Gradle version", "android", "gradle-version")
    canary.step("Release notes headings", "release-notes", "--check-headings")
    canary.step("Import the store tooling", "smoke")
    canary.step("Test screenshot staging with Pillow", "selftest", "--pillow-only", "--require-pillow")
    staged = work / "staged-for-upload"
    if canary.step("Stage every screenshot set for upload", "screenshots", "stage", "--platform", "all", "--out",
                   str(staged), env={"STORE_UPLOAD_SCREENSHOTS": "true"}):
        for folder, (count, width, height) in (expectations.get("screenshots") or {}).items():
            files = sorted((staged / folder).glob("*.png"))
            canary.expect(len(files) == count, f"{folder}: {len(files)} staged frame(s), expected {count}")
            for path in files:
                found_width, found_height, colour = png_header(path)
                canary.expect((found_width, found_height) == (width, height),
                              f"{folder}/{path.name} is {found_width}x{found_height}, expected {width}x{height}")
                if folder.startswith("ios/"):
                    canary.expect(colour == 2, f"{folder}/{path.name} is RGB without alpha (colour type {colour})")
    creative_root = app_root / (expectations.get("creative_dir") or "store/creative/ios")
    for relative, size in (expectations.get("creative") or {}).items():
        path = creative_root / relative
        found = path.stat().st_size if path.is_file() else None
        canary.expect(found == size, f"creative {relative}: {found} bytes, expected {size} "
                                     "(the Asset Library matches images by name and size)")
    if canary.failures:
        print(f"canary: {len(canary.failures)} failure(s):")
        for failure in canary.failures:
            print(f"  {failure}")
        return 1
    print("canary: every store check passed on the app's main branch")
    return 0


def main(app: Any, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="canary", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect", type=Path, default=TOOL_ROOT / "tests" / "canary" / "fud-ai.toml")
    args = parser.parse_args(argv)
    expectations = tomllib.loads(args.expect.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="store-release-canary-") as work:
        return run(app.root, expectations, Path(work))
