#!/usr/bin/env python3
"""Offline tests for ABYSM's iOS release switches, build waiting, listing and review.

No network: every App Store Connect call goes to an in-memory fake. Also checks
that scripts/test-ios.sh shuts the simulator down between the unit and UI tests,
against a fake xcrun (no simulator).
Run: python3 -m unittest discover -s scripts/store -p 'test_*.py'

Release runs (ios-release.yml's prepare job) set STORE_TESTS_SCOPE=release. The
tests that check repository content a release may not use (the listing and
screenshots, other tags' release notes) are then skipped: a release checks the
listing and screenshots only when their switch is on (the gated
`asc_release.py --dry-run`) and only its own release notes, so unused files
never block a build-only release. Pull requests and main (store-checks.yml) run
every test.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import http.client
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
import urllib.parse
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

import asc_release  # noqa: E402
import export_listing  # noqa: E402
import ios_release  # noqa: E402
import print_gates  # noqa: E402
import redact_logs  # noqa: E402
import release_notes  # noqa: E402
import validate_catalog  # noqa: E402
from ios_release import ReleaseError, resolve_gates  # noqa: E402

RELEASE_SCOPE = os.environ.get("STORE_TESTS_SCOPE") == "release"
# Checks of repository content a release may not use: the listing and screenshots
# (checked by the gated dry run when their switch is on) and other tags' release
# notes (the tag's own section is checked by the "Check release notes" step).
repo_content = unittest.skipIf(RELEASE_SCOPE, "release run: checked only when this release uses it")
REPO_CONTENT_TESTS = (
    "ScreenshotPlanTests.test_repo_marketing_images_are_app_store_ready",
    "ListingTests.test_repo_metadata_is_valid_for_every_switch_but_whats_new",
    "AscCliTests.test_full_dry_run_with_repo_inputs",
    "CreativeAssetTests.test_the_repository_images_pass_the_local_checks",
    "ReleaseNotesTests.test_repository_release_notes_have_one_section_per_heading",
)

TAG = "v1.0.1"
VERSION = "1.0.1"
APP = asc_release.APP_STORE_APP_ID
ALL_ON = {
    "IOS_RELEASE_AUTOMATION": "true",
    "IOS_CREATE_VERSION": "true",
    "IOS_UPLOAD_BUILD": "true",
    "IOS_UPLOAD_LISTING": "true",
    "IOS_UPLOAD_WHATS_NEW": "true",
    "IOS_UPLOAD_SCREENSHOTS": "true",
    "IOS_SUBMIT_REVIEW": "true",
    "IOS_PUBLIC_RELEASE": "true",
}
OPS = ("create_version", "upload_build", "listing", "whats_new", "screenshots", "submit", "public_release")
ASC_FLAGS = {"UPLOAD_LISTING", "UPLOAD_WHATS_NEW", "UPLOAD_SCREENSHOTS", "UPLOAD_CREATIVE_ASSETS",
             "SUBMIT_IOS_REVIEW", "PUBLIC_RELEASE", "CREATE_IOS_VERSION"}

# Pillow saves the RGB screenshot copies. The Store release checks install it and
# set STORE_TESTS_NEED_PILLOW, so the tests that need it can't skip there.
PILLOW = asc_release.try_import_pillow()
needs_pillow = unittest.skipIf(
    PILLOW is None and not os.environ.get("STORE_TESTS_NEED_PILLOW"), "Pillow not installed"
)


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


@functools.lru_cache(maxsize=None)
def black_pixels(width: int, height: int, channels: int) -> bytes:
    return zlib.compress((b"\x00" + b"\x00" * (width * channels)) * height)


def make_png(width: int, height: int, colour_type: int = 2, *, chunks: tuple[tuple[bytes, bytes], ...] = ()) -> bytes:
    """A valid PNG of black pixels (8 bits per sample), built without Pillow.

    chunks are extra (type, data) chunks written before the image data, e.g. tRNS.
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, colour_type, 0, 0, 0])
    extra = b"".join(chunk(kind, data) for kind, data in chunks)
    palette = chunk(b"PLTE", b"\x00\x00\x00") if colour_type == 3 else b""
    pixels = black_pixels(width, height, channels)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + palette + extra
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


def marked_png(width: int, height: int, marker: str, colour_type: int = 2) -> bytes:
    """make_png with a text chunk, so files of one size have their own checksums."""
    return make_png(width, height, colour_type, chunks=((b"tEXt", b"marker\x00" + marker.encode()),))


def gates(env: dict[str, str], *, event: str = "push", ref: str = TAG, version: str = VERSION) -> dict:
    return resolve_gates(env, event=event, ref_name=ref, marketing_version=version)


class GateTests(unittest.TestCase):
    def test_missing_settings_mean_everything_off(self) -> None:
        result = gates({})
        self.assertEqual(result["mode"], "release")
        self.assertFalse(result["master"])
        self.assertFalse(result["build"])
        self.assertFalse(result["asc"])
        for key in OPS:
            self.assertFalse(result[key], key)
        self.assertEqual((result["poll_seconds"], result["wait_minutes"]), (30, 120))

    def test_master_off_ignores_operation_switches(self) -> None:
        result = gates({**ALL_ON, "IOS_RELEASE_AUTOMATION": "false"})
        for key in OPS:
            self.assertFalse(result[key], key)
        self.assertFalse(result["build"])
        self.assertTrue(any("ignored" in note for note in result["notes"]))

    def test_each_operation_is_independent(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "yes", "IOS_UPLOAD_LISTING": "on"})
        self.assertTrue(result["listing"])
        self.assertTrue(result["asc"])
        self.assertFalse(result["whats_new"])
        self.assertFalse(result["upload_build"])
        self.assertFalse(result["build"])
        result = gates({"IOS_RELEASE_AUTOMATION": "1", "IOS_UPLOAD_BUILD": "1"})
        self.assertTrue(result["build"])
        self.assertFalse(result["asc"])

    def test_all_on(self) -> None:
        result = gates(ALL_ON)
        for key in OPS:
            self.assertTrue(result[key], key)
        self.assertTrue(result["build"])
        self.assertTrue(result["master"])

    def test_invalid_values_are_rejected_even_when_master_is_off(self) -> None:
        for name in ("IOS_RELEASE_AUTOMATION", "IOS_UPLOAD_BUILD", "IOS_SUBMIT_REVIEW", "IOS_UPLOAD_WHATS_NEW"):
            for bad in ("maybe", "enabled", "2", "tru"):
                with self.subTest(name=name, bad=bad), self.assertRaisesRegex(ReleaseError, name):
                    gates({name: bad})

    def test_switch_values_are_case_and_space_insensitive(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": " TRUE ", "IOS_UPLOAD_LISTING": "Yes"})
        self.assertTrue(result["listing"])
        self.assertFalse(gates({"IOS_RELEASE_AUTOMATION": "OFF"})["master"])

    def test_bad_timing_values_are_rejected(self) -> None:
        for env in (
            {"IOS_BUILD_POLL_SECONDS": "5"},
            {"IOS_BUILD_POLL_SECONDS": "601"},
            {"IOS_BUILD_POLL_SECONDS": "thirty"},
            {"IOS_BUILD_POLL_SECONDS": "٣٠"},
            {"IOS_BUILD_WAIT_MINUTES": "9"},
            {"IOS_BUILD_WAIT_MINUTES": "301"},
            {"IOS_BUILD_WAIT_MINUTES": "-5"},
        ):
            with self.subTest(env=env), self.assertRaises(ReleaseError):
                gates(env)
        result = gates({"IOS_BUILD_POLL_SECONDS": "45", "IOS_BUILD_WAIT_MINUTES": "90"})
        self.assertEqual((result["poll_seconds"], result["wait_minutes"]), (45, 90))
        result = gates({"IOS_BUILD_POLL_SECONDS": "10", "IOS_BUILD_WAIT_MINUTES": "300"})
        self.assertEqual((result["poll_seconds"], result["wait_minutes"]), (10, 300))

    def test_create_version_needs_only_the_master_switch(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "true", "IOS_CREATE_VERSION": "true"})
        self.assertTrue(result["create_version"])
        self.assertFalse(result["build"])
        self.assertFalse(result["asc"])
        self.assertFalse(gates({"IOS_CREATE_VERSION": "true"})["create_version"])
        self.assertFalse(gates(ALL_ON, event="workflow_dispatch", ref="main")["create_version"])
        with self.assertRaisesRegex(ReleaseError, "IOS_CREATE_VERSION"):
            gates({"IOS_CREATE_VERSION": "sometimes"})

    def test_submit_requires_this_runs_build(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "IOS_UPLOAD_BUILD"):
            gates({"IOS_RELEASE_AUTOMATION": "true", "IOS_SUBMIT_REVIEW": "true"})

    def test_public_release_requires_submit(self) -> None:
        env = {"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_BUILD": "true", "IOS_PUBLIC_RELEASE": "true"}
        with self.assertRaisesRegex(ReleaseError, "IOS_SUBMIT_REVIEW"):
            gates(env)

    def test_tag_must_be_exactly_v_plus_marketing_version(self) -> None:
        for ref in ("v1.0.2", "v1.0", "v1.0.1-beta.1", "v01.0.1", "v1.0.1.0"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ReleaseError, "does not match"):
                gates({}, ref=ref)
        for ref in ("1.0.1", "ios-v1.0.1", "release-1.0.1", ""):
            with self.subTest(ref=ref), self.assertRaisesRegex(ReleaseError, "tag"):
                gates({}, ref=ref)
        # The historical v1.0.0 tag no longer matches a 1.0 project.
        with self.assertRaisesRegex(ReleaseError, "does not match"):
            gates({}, ref="v1.0.0", version="1.0")
        self.assertEqual(gates({}, ref="v1.0", version="1.0")["marketing_version"], "1.0")

    def test_tag_mismatch_fails_even_with_master_off(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "does not match"):
            gates({"IOS_RELEASE_AUTOMATION": "false"}, ref="v9.9")

    def test_manual_run_validates_only(self) -> None:
        result = gates(ALL_ON, event="workflow_dispatch", ref="main")
        self.assertEqual(result["mode"], "validate")
        self.assertTrue(result["build"])
        self.assertFalse(result["master"])
        self.assertFalse(result["asc"])
        for key in OPS:
            self.assertFalse(result[key], key)

    def test_manual_run_still_rejects_invalid_switches(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "IOS_UPLOAD_LISTING"):
            gates({"IOS_UPLOAD_LISTING": "sure"}, event="workflow_dispatch", ref="main")

    def test_manual_run_can_validate_a_future_version(self) -> None:
        result = resolve_gates({}, event="workflow_dispatch", ref_name="main",
                               marketing_version="1.0", validation_version="1.0.1")
        self.assertEqual(result["mode"], "validate")
        self.assertEqual(result["marketing_version"], "1.0.1")
        self.assertFalse(result["asc"])
        self.assertTrue(any("not changed" in note for note in result["notes"]))
        default = resolve_gates({}, event="workflow_dispatch", ref_name="main", marketing_version="1.0")
        self.assertEqual(default["marketing_version"], "1.0")

    def test_version_override_is_rejected_for_releases_and_bad_values(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "manual validation"):
            resolve_gates({}, event="push", ref_name=TAG, marketing_version=VERSION, validation_version="1.0.2")
        for bad in ("1.x", "v1.0.1", "1.0.1.2", "1.0 beta", "١.٠"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ReleaseError, "must look like"):
                resolve_gates({}, event="workflow_dispatch", ref_name="main",
                              marketing_version=VERSION, validation_version=bad)

    def test_unsupported_event_fails(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "unsupported event"):
            gates({}, event="pull_request")


PBX_TEMPLATE = """
		AAA /* Debug */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				CURRENT_PROJECT_VERSION = {debug_build};
				MARKETING_VERSION = {debug_version};
				PRODUCT_BUNDLE_IDENTIFIER = com.apoorvdarshan.Abysm;
			}};
			name = Debug;
		}};
		BBB /* Release */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				CURRENT_PROJECT_VERSION = {release_build};
				LD_RUNPATH_SEARCH_PATHS = (
					"$(inherited)",
					"@executable_path/Frameworks",
				);
				MARKETING_VERSION = "{release_version}";
				PRODUCT_BUNDLE_IDENTIFIER = com.apoorvdarshan.Abysm;
			}};
			name = Release;
		}};
		CCC /* Debug */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				CURRENT_PROJECT_VERSION = 1;
				MARKETING_VERSION = 0.9;
				PRODUCT_BUNDLE_IDENTIFIER = com.apoorvdarshan.AbysmTests;
			}};
			name = Debug;
		}};
"""


class ProjectVersionTests(unittest.TestCase):
    def pbx(self, **values: str) -> Path:
        settings = {"debug_build": "7", "release_build": "7", "debug_version": "1.2", "release_version": "1.2"}
        settings.update(values)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".pbxproj", delete=False)
        tmp.write(PBX_TEMPLATE.format(**settings))
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_real_project_uses_the_app_target_only(self) -> None:
        version = ios_release.project_marketing_version()
        self.assertRegex(version, r"^\d+(\.\d+){1,2}$")
        self.assertGreaterEqual(ios_release.project_build_number(), 1)
        configs = ios_release.app_build_configurations()
        self.assertEqual(set(configs), {"Debug", "Release"})

    def test_test_targets_do_not_affect_the_release_version(self) -> None:
        path = self.pbx()
        self.assertEqual(ios_release.project_marketing_version(path), "1.2")
        self.assertEqual(ios_release.project_build_number(path), 7)

    def test_debug_and_release_must_agree(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "MARKETING_VERSION"):
            ios_release.project_marketing_version(self.pbx(release_version="1.3"))
        with self.assertRaisesRegex(ReleaseError, "CURRENT_PROJECT_VERSION"):
            ios_release.project_build_number(self.pbx(debug_build="8"))

    def test_bad_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "must look like"):
            ios_release.project_marketing_version(self.pbx(debug_version="$(VERSION)", release_version="$(VERSION)"))
        with self.assertRaisesRegex(ReleaseError, "whole number"):
            ios_release.project_build_number(self.pbx(debug_build="7a", release_build="7a"))

    def test_missing_app_target_fails(self) -> None:
        path = self.pbx()
        with self.assertRaisesRegex(ReleaseError, "no build configuration"):
            ios_release.app_build_configurations(path, bundle_id="com.example.other")


COMPLIANCE_PBX = """
		AAA /* Debug */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
{debug}				PRODUCT_BUNDLE_IDENTIFIER = com.apoorvdarshan.Abysm;
			}};
			name = Debug;
		}};
		BBB /* Release */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
{release}				PRODUCT_BUNDLE_IDENTIFIER = {release_bundle};
			}};
			name = Release;
		}};
		CCC /* Release */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
{other}				PRODUCT_BUNDLE_IDENTIFIER = com.apoorvdarshan.AbysmTests;
			}};
			name = Release;
		}};
"""
NO = "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = NO"
YES = "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = YES"
CODE = "INFOPLIST_KEY_ITSEncryptionExportComplianceCode = ABC123"
PLIST_FILE = "INFOPLIST_FILE = Abysm/Info.plist"


class ExportComplianceTests(unittest.TestCase):
    """ITSAppUsesNonExemptEncryption must be declared, or App Store Connect asks about every build."""

    def project(self, debug: list[str], release: list[str] | None = None, plist: dict | None = None, *,
                other: tuple[str, ...] = ("INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = YES",),
                release_bundle: str = "com.apoorvdarshan.Abysm") -> Path:
        """A temporary project (app Debug/Release + a test target with `other` settings) and its Abysm/Info.plist."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if plist is not None:
            (root / "Abysm").mkdir()
            (root / "Abysm" / "Info.plist").write_bytes(plistlib.dumps(plist))
        lines = lambda settings: "".join(f"\t\t\t\t{line};\n" for line in settings)  # noqa: E731
        pbxproj = root / "Abysm.xcodeproj" / "project.pbxproj"
        pbxproj.parent.mkdir()
        pbxproj.write_text(COMPLIANCE_PBX.format(debug=lines(debug), release=lines(debug if release is None else release),
                                                 other=lines(other), release_bundle=release_bundle))
        return pbxproj

    def check(self, *args, **kwargs) -> str:
        return ios_release.check_export_compliance(self.project(*args, **kwargs))

    def info_plist(self, values: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "Abysm.app" / "Info.plist"
        path.parent.mkdir()
        path.write_bytes(plistlib.dumps({"CFBundleIdentifier": ios_release.BUNDLE_ID, **values},
                                        fmt=plistlib.FMT_BINARY))  # archived apps carry binary plists
        return path

    def test_the_real_app_declares_no_non_exempt_encryption(self) -> None:
        self.assertEqual(sorted(ios_release.app_build_configurations()), ["Debug", "Release"])
        self.assertEqual(ios_release.check_export_compliance(), "NO")
        with (ROOT / "Abysm" / "Info.plist").open("rb") as handle:
            self.assertIs(plistlib.load(handle)[ios_release.ENCRYPTION_KEY], False)

    def test_no_from_the_build_setting_or_the_info_plist_passes(self) -> None:
        self.assertEqual(self.check([NO]), "NO")
        self.assertEqual(self.check([PLIST_FILE], plist={"ITSAppUsesNonExemptEncryption": False}), "NO")
        self.assertEqual(self.check([NO, PLIST_FILE], plist={"ITSAppUsesNonExemptEncryption": False}), "NO")
        self.assertEqual(self.check(['INFOPLIST_FILE = "$(SRCROOT)/Abysm/Info.plist"'],
                                    plist={"ITSAppUsesNonExemptEncryption": False}), "NO")

    def test_missing_declaration_fails_before_the_build(self) -> None:
        for release, missing in ((None, "Debug, Release"), ([NO, PLIST_FILE], "Debug")):
            with self.subTest(release=release), self.assertRaisesRegex(
                    ReleaseError, rf"the app target \(com\.apoorvdarshan\.Abysm\) does not declare export "
                                  rf"compliance \({missing}\)"):
                self.check([PLIST_FILE], release, plist={"UILaunchScreen": {}})
        with self.assertRaisesRegex(ReleaseError, "does not declare export compliance"):
            self.check(["INFOPLIST_KEY_CFBundleDisplayName = ABYSM"])

    def test_a_value_set_only_for_debug_or_only_on_another_target_fails(self) -> None:
        # Debug alone, or Release alone, never stands in for the other configuration.
        with self.assertRaisesRegex(ReleaseError, r"does not declare export compliance \(Release\)"):
            self.check([NO], [])
        with self.assertRaisesRegex(ReleaseError, r"does not declare export compliance \(Debug\)"):
            self.check([], [NO])
        # Another target's answer (here the tests' Release configuration) is not the app's.
        with self.assertRaisesRegex(ReleaseError, r"does not declare export compliance \(Debug, Release\)"):
            self.check([], other=(NO,))
        self.assertEqual(self.check([NO], other=(YES,)), "NO")  # the tests' YES without a code is ignored

    def test_both_app_configurations_must_exist(self) -> None:
        # A Release configuration that does not build the app's bundle id cannot be
        # checked, so a Debug-only answer never passes for it.
        for bundle in ("$(APP_BUNDLE_ID)", "com.apoorvdarshan.Abysm.dev"):
            with self.subTest(bundle=bundle), self.assertRaisesRegex(
                    ReleaseError, r"could not find the Debug and Release configurations .*found \['Debug'\]"):
                self.check([NO], release_bundle=bundle)
        with self.assertRaisesRegex(ReleaseError, "no build configuration builds com.example.Other"):
            ios_release.check_export_compliance(self.project([NO]), bundle_id="com.example.Other")

    def test_yes_needs_the_compliance_code(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "needs ITSEncryptionExportComplianceCode"):
            self.check([YES])
        with self.assertRaisesRegex(ReleaseError, "missing in .*: Release"):
            self.check([YES, CODE], [YES])
        self.assertEqual(self.check([YES, CODE]), "YES")
        self.assertEqual(self.check([PLIST_FILE], plist={"ITSAppUsesNonExemptEncryption": True,
                                                         "ITSEncryptionExportComplianceCode": "ABC123"}), "YES")

    def test_mixed_or_unknown_values_fail(self) -> None:
        cases = {
            "Debug NO, Release YES": ([NO], [YES, CODE], None),
            "build setting and Info.plist disagree": ([NO, PLIST_FILE], None, {"ITSAppUsesNonExemptEncryption": True}),
            "unknown build setting": (["INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = maybe"], None, None),
            "Info.plist string instead of a Boolean": ([PLIST_FILE], None, {"ITSAppUsesNonExemptEncryption": "NO"}),
        }
        for label, (debug, release, plist) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ReleaseError, "must be one NO or YES"):
                self.check(debug, release, plist=plist)

    def test_an_info_plist_that_cannot_be_read_fails(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "cannot read"):
            self.check([NO, PLIST_FILE])  # the file does not exist
        with self.assertRaisesRegex(ReleaseError, "cannot resolve INFOPLIST_FILE"):
            self.check([NO, "INFOPLIST_FILE = $(TARGET_NAME)/Info.plist"])

    def test_the_archived_info_plist_is_checked_the_same_way(self) -> None:
        check = ios_release.check_bundle_export_compliance
        self.assertEqual(check(self.info_plist({"ITSAppUsesNonExemptEncryption": False})), "NO")
        self.assertEqual(check(self.info_plist({"ITSAppUsesNonExemptEncryption": True,
                                                "ITSEncryptionExportComplianceCode": "ABC123"})), "YES")
        for values, message in (({}, r"the archived Abysm.app does not declare"),
                                ({"ITSAppUsesNonExemptEncryption": True}, "needs ITSEncryptionExportComplianceCode"),
                                ({"ITSAppUsesNonExemptEncryption": "false"}, "must be one NO or YES")):
            with self.subTest(values=values), self.assertRaisesRegex(ReleaseError, message):
                check(self.info_plist(values))

    def test_the_command_line_check_fails_the_step(self) -> None:
        script = str(HERE / "ios_release.py")
        good = self.info_plist({"ITSAppUsesNonExemptEncryption": False})
        bad = self.info_plist({})
        result = subprocess.run([sys.executable, script, "export-compliance", "--info-plist", str(good)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ITSAppUsesNonExemptEncryption = NO (no non-exempt encryption)", result.stdout)
        result = subprocess.run([sys.executable, script, "export-compliance", "--info-plist", str(bad)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: the archived Abysm.app does not declare export compliance", result.stderr)
        result = subprocess.run([sys.executable, script, "export-compliance"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gates_check_it_only_when_this_run_builds(self) -> None:
        for env, builds in (({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main"}, True),
                            ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": TAG, "IOS_RELEASE_AUTOMATION": "true",
                              "IOS_UPLOAD_BUILD": "true"}, True),
                            ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": TAG}, False)):
            missing = mock.patch.object(ios_release, "check_export_compliance",
                                        side_effect=ReleaseError("Abysm does not declare export compliance"))
            with self.subTest(env=env), missing as check, \
                    mock.patch.object(ios_release, "project_marketing_version", return_value=VERSION), \
                    mock.patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()) as err:
                if builds:
                    with self.assertRaises(SystemExit):
                        ios_release.main(["gates"])
                    self.assertIn("does not declare export compliance", err.getvalue())
                else:
                    ios_release.main(["gates"])
                    check.assert_not_called()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main"},
                             clear=True), redirect_stdout(out):
            ios_release.main(["gates"])
        self.assertIn("export compliance: ITSAppUsesNonExemptEncryption = NO", out.getvalue())


class BuildNumberTests(unittest.TestCase):
    def test_next_build_number(self) -> None:
        self.assertEqual(ios_release.next_build_number(2, ["1", "2"]), 3)
        self.assertEqual(ios_release.next_build_number(9, ["1", "2"]), 9)
        self.assertEqual(ios_release.next_build_number(1, []), 1)
        self.assertEqual(ios_release.next_build_number(5, ["7.1", "abc", "9"]), 10)
        self.assertEqual(ios_release.next_build_number(5, ["٩٩"]), 5)  # only ASCII digits count


STARTED = 1_800_000_000.0  # this run's upload start (epoch seconds)


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def build_row(build_id: str = "B1", *, number: str = "3", state: str = "VALID", expired: bool = False,
              uploaded: float | str | None = STARTED + 60, app: str = APP, pre: str = "P1") -> dict:
    attributes = {"version": number, "processingState": state, "expired": expired}
    if uploaded is not None:
        attributes["uploadedDate"] = uploaded if isinstance(uploaded, str) else iso(uploaded)
    return {
        "type": "builds",
        "id": build_id,
        "attributes": attributes,
        "relationships": {
            "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": pre}},
            "app": {"data": {"type": "apps", "id": app}},
        },
    }


def pre_row(pre_id: str = "P1", version: str = VERSION, platform: str = "IOS") -> dict:
    return {"type": "preReleaseVersions", "id": pre_id, "attributes": {"version": version, "platform": platform}}


class FakeClock:
    def __init__(self, start: float) -> None:
        self.time = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


class SequenceClient:
    """Returns queued /builds payloads (or raises queued exceptions) per GET."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.paths: list[str] = []

    def get(self, path: str) -> dict:
        self.paths.append(path)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def wait(client: SequenceClient, clock: FakeClock, *, minutes: int = 120, transient=None) -> dict:
    with redirect_stdout(io.StringIO()):
        return ios_release.wait_for_build(
            client, app_id=APP, marketing_version=VERSION, build_number="3", uploaded_after=STARTED,
            deadline=STARTED + minutes * 60, poll_seconds=30, now=clock.now, sleep=clock.sleep,
            is_transient=transient,
        )


class WaitForBuildTests(unittest.TestCase):
    def test_waits_through_missing_and_processing_then_returns_build(self) -> None:
        client = SequenceClient([
            {"data": []},
            {"data": [build_row(state="PROCESSING")], "included": [pre_row()]},
            {"data": [build_row()], "included": [pre_row()]},
        ])
        clock = FakeClock(STARTED + 120)
        self.assertEqual(wait(client, clock)["id"], "B1")
        self.assertEqual(clock.sleeps, [30, 30])
        self.assertIn("filter%5Bversion%5D=3", client.paths[0])
        self.assertIn(f"filter%5Bapp%5D={APP}", client.paths[0])

    def test_times_out_without_using_another_build(self) -> None:
        client = SequenceClient([{"data": [build_row(state="PROCESSING")], "included": [pre_row()]}])
        clock = FakeClock(STARTED)
        with self.assertRaisesRegex(ReleaseError, "timed out"):
            wait(client, clock, minutes=10)
        self.assertLessEqual(clock.time, STARTED + 10 * 60 + 30)

    def test_ready_build_after_the_deadline_is_still_a_timeout(self) -> None:
        client = SequenceClient([{"data": [build_row()], "included": [pre_row()]}])
        clock = FakeClock(STARTED + 121 * 60)
        with self.assertRaisesRegex(ReleaseError, "timed out"):
            wait(client, clock)
        self.assertEqual(client.paths, [], "must not poll once the budget is spent")

    def test_processing_failure_stops(self) -> None:
        for state in ("FAILED", "INVALID"):
            client = SequenceClient([{"data": [build_row(state=state)], "included": [pre_row()]}])
            with self.subTest(state=state), self.assertRaisesRegex(ReleaseError, state):
                wait(client, FakeClock(STARTED))

    def test_expired_build_is_rejected(self) -> None:
        client = SequenceClient([{"data": [build_row(expired=True)], "included": [pre_row()]}])
        with self.assertRaisesRegex(ReleaseError, "expired"):
            wait(client, FakeClock(STARTED))

    def test_ambiguous_match_stops(self) -> None:
        client = SequenceClient([{"data": [build_row("B1"), build_row("B2")], "included": [pre_row()]}])
        with self.assertRaisesRegex(ReleaseError, "ambiguous"):
            wait(client, FakeClock(STARTED))

    def test_older_upload_is_never_substituted(self) -> None:
        client = SequenceClient([{"data": [build_row(uploaded=STARTED - 3600)], "included": [pre_row()]}])
        with self.assertRaisesRegex(ReleaseError, "older build"):
            wait(client, FakeClock(STARTED))

    def test_build_without_an_upload_time_is_never_accepted(self) -> None:
        # Fail closed: it could be an older build with the same number.
        for missing in (None, ""):
            client = SequenceClient([{"data": [build_row(uploaded=missing)], "included": [pre_row()]}])
            with self.subTest(uploaded=missing), self.assertRaisesRegex(ReleaseError, "timed out"):
                wait(client, FakeClock(STARTED), minutes=10)
        state, message, build = ios_release.evaluate_builds(
            {"data": [build_row(uploaded=None)], "included": [pre_row()]},
            app_id=APP, marketing_version=VERSION, build_number="3", uploaded_after=STARTED)
        self.assertEqual((state, build), (ios_release.BuildState.WAITING, None))
        self.assertIn("uploadedDate", message)

    def test_upload_time_reported_later_is_accepted(self) -> None:
        client = SequenceClient([
            {"data": [build_row(uploaded=None)], "included": [pre_row()]},
            {"data": [build_row()], "included": [pre_row()]},
        ])
        clock = FakeClock(STARTED)
        self.assertEqual(wait(client, clock)["id"], "B1")
        self.assertEqual(clock.sleeps, [30])

    def test_unreadable_upload_time_stops(self) -> None:
        for bad in ("yesterday", 1_800_000_000):
            client = SequenceClient([{"data": [build_row(uploaded=None)], "included": [pre_row()]}])
            client.responses[0]["data"][0]["attributes"]["uploadedDate"] = bad
            with self.subTest(uploaded=bad), self.assertRaisesRegex(ReleaseError, "unreadable uploadedDate"):
                wait(client, FakeClock(STARTED))

    def test_other_versions_and_platforms_are_ignored(self) -> None:
        other_version = build_row("BX", pre="P2")
        other_platform = build_row("BY", pre="P3")
        client = SequenceClient([
            {"data": [other_version, other_platform], "included": [pre_row("P2", "1.0"), pre_row("P3", VERSION, "MAC_OS")]},
            {"data": [other_version, build_row("B1")], "included": [pre_row("P2", "1.0"), pre_row()]},
        ])
        self.assertEqual(wait(client, FakeClock(STARTED))["id"], "B1")

    def test_build_from_another_app_is_a_mismatch(self) -> None:
        client = SequenceClient([{"data": [build_row(app="OTHER")], "included": [pre_row()]}])
        with self.assertRaisesRegex(ReleaseError, "another app"):
            wait(client, FakeClock(STARTED))

    def test_transient_errors_are_retried_and_others_raise(self) -> None:
        transient = asc_release.AscHttpError(503, "unavailable")
        client = SequenceClient([transient, {"data": [build_row()], "included": [pre_row()]}])
        with redirect_stderr(io.StringIO()):
            build = wait(client, FakeClock(STARTED), transient=ios_release.is_transient_error)
        self.assertEqual(build["id"], "B1")
        fatal = SequenceClient([asc_release.AscHttpError(401, "unauthorized")])
        with self.assertRaises(SystemExit):
            wait(fatal, FakeClock(STARTED), transient=ios_release.is_transient_error)


class TransientErrorTests(unittest.TestCase):
    def test_retry_rule(self) -> None:
        transient = [
            http.client.RemoteDisconnected("closed"),
            ConnectionResetError("reset"),
            http.client.IncompleteRead(b""),
            urllib.error.URLError("dns"),
            TimeoutError("slow"),
            asc_release.AscHttpError(503, "busy"),
            asc_release.AscHttpError(429, "rate limited"),
            urllib.error.HTTPError("https://x", 502, "bad gateway", {}, None),
        ]
        fatal = [
            asc_release.AscHttpError(401, "unauthorized"),
            asc_release.AscHttpError(409, "conflict"),
            urllib.error.HTTPError("https://x", 404, "missing", {}, None),
            ValueError("bad"),
            ReleaseError("mismatch"),
            SystemExit(1),
        ]
        for exc in transient:
            with self.subTest(exc=repr(exc)):
                self.assertTrue(ios_release.is_transient_error(exc))
        for exc in fatal:
            with self.subTest(exc=repr(exc)):
                self.assertFalse(ios_release.is_transient_error(exc))


class CmdWaitBuildTests(unittest.TestCase):
    def test_deadline_counts_from_upload_start_and_app_lookup_is_retried(self) -> None:
        lookups = []

        def find_app(_client, bundle_id):
            lookups.append(bundle_id)
            if len(lookups) == 1:
                raise http.client.RemoteDisconnected("closed")
            return APP

        captured = {}

        def fake_wait(client, **kwargs):
            captured.update(kwargs)
            return {"id": "B9"}

        args = argparse.Namespace(version=VERSION, build=3, started_at=STARTED, wait_minutes=120, poll_seconds=30)
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, \
                mock.patch.object(ios_release, "asc_client", return_value=(mock.Mock(find_app=find_app), object())), \
                mock.patch.object(ios_release, "wait_for_build", side_effect=fake_wait), \
                mock.patch.object(ios_release.time, "sleep"), \
                mock.patch.object(ios_release.time, "time", return_value=STARTED + 30), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out.name}), redirect_stdout(io.StringIO()):
            ios_release.cmd_wait_build(args)
            written = Path(out.name).read_text()
        self.assertEqual(lookups, [ios_release.BUNDLE_ID] * 2)
        self.assertEqual(captured["deadline"], STARTED + 120 * 60)
        self.assertEqual(captured["uploaded_after"], STARTED)
        self.assertEqual(captured["build_number"], "3")
        self.assertIs(captured["is_transient"], ios_release.is_transient_error)
        self.assertIn("build_id=B9", written)

    def test_app_lookup_gives_up_at_the_deadline(self) -> None:
        def boom():
            raise http.client.RemoteDisconnected("closed")

        clock = FakeClock(STARTED)
        with redirect_stdout(io.StringIO()), self.assertRaises(http.client.RemoteDisconnected):
            ios_release.call_with_retries(boom, deadline=STARTED + 90, poll_seconds=30, now=clock.now, sleep=clock.sleep)
        self.assertEqual(clock.sleeps, [30, 30, 30])


class CmdBuildNumberTests(unittest.TestCase):
    def test_uses_the_highest_uploaded_build_plus_one(self) -> None:
        client = mock.Mock()
        client.get.side_effect = [
            {"data": [{"id": APP, "attributes": {"bundleId": ios_release.BUNDLE_ID}}]},
            {"data": [{"attributes": {"version": "1"}}, {"attributes": {"version": "2"}}],
             "links": {"next": "https://api.appstoreconnect.apple.com/v1/builds?cursor=2"}},
            {"data": [{"attributes": {"version": "5"}}]},
        ]
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, \
                mock.patch.object(ios_release, "asc_client", return_value=(asc_release, client)), \
                mock.patch.object(ios_release, "project_build_number", return_value=2), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out.name}), redirect_stdout(io.StringIO()):
            ios_release.cmd_build_number(argparse.Namespace())
            self.assertIn("build_number=6", Path(out.name).read_text())


class FindAppTests(unittest.TestCase):
    def client(self, rows: list[dict]) -> mock.Mock:
        client = mock.Mock()
        client.get.return_value = {"data": rows}
        return client

    def test_returns_abysm_only(self) -> None:
        row = {"id": APP, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}
        self.assertEqual(asc_release.find_app(self.client([row]), asc_release.DEFAULT_BUNDLE_ID), APP)

    def test_wrong_missing_or_ambiguous_app_fails(self) -> None:
        row = {"id": APP, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}
        cases = {
            "other id": [{"id": "999", "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}],
            "missing": [],
            "prefix only": [{"id": APP, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID + ".beta"}}],
            "ambiguous": [row, dict(row)],
            "two apps with the bundle id": [row, {"id": "999", "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}],
            "no bundle id reported": [{"id": APP}],
        }
        for label, rows in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                quiet(asc_release.find_app, self.client(rows), asc_release.DEFAULT_BUNDLE_ID)


class SelectionClient:
    """Fake ASC client for build verification and selection."""

    def __init__(self, *, build: dict, included: list, attached: str | None, sticky: bool = True) -> None:
        self.build = build
        self.included = included
        self.attached = attached
        self.sticky = sticky
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path, None))
        if path.startswith(f"/builds/{self.build['id']}"):
            return {"data": self.build, "included": self.included}
        if path.endswith("/build"):
            return {"data": {"type": "builds", "id": self.attached} if self.attached else None}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path, body))
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        if method == "PATCH" and path.endswith("/relationships/build") and self.sticky:
            self.attached = body["data"]["id"]
        return None


class BuildSelectionTests(unittest.TestCase):
    def test_selects_and_reads_back_this_runs_build(self) -> None:
        client = SelectionClient(build=build_row("B9"), included=[pre_row()], attached="OLD")
        quiet(asc_release.select_build, client, "V1", "B9", VERSION, "3")
        self.assertEqual(client.attached, "B9")
        methods = [(m, p) for m, p, _ in client.calls]
        self.assertIn(("PATCH", "/appStoreVersions/V1/relationships/build"), methods)
        self.assertEqual(methods[-1], ("GET", "/appStoreVersions/V1/build"))

    def test_selection_that_does_not_stick_fails(self) -> None:
        client = SelectionClient(build=build_row("B9"), included=[pre_row()], attached="OLD", sticky=False)
        with self.assertRaises(SystemExit):
            quiet(asc_release.select_build, client, "V1", "B9", VERSION, "3")

    def test_unprocessed_expired_or_wrong_build_is_refused(self) -> None:
        cases = {
            "processing": (build_row("B9", state="PROCESSING"), [pre_row()]),
            "expired": (build_row("B9", expired=True), [pre_row()]),
            "number": (build_row("B9", number="4"), [pre_row()]),
            "version": (build_row("B9"), [pre_row(version="1.0")]),
            "platform": (build_row("B9"), [pre_row(platform="TV_OS")]),
        }
        for label, (build, included) in cases.items():
            client = SelectionClient(build=build, included=included, attached=None)
            with self.subTest(label), self.assertRaises(SystemExit):
                quiet(asc_release.select_build, client, "V1", "B9", VERSION, "3")
            self.assertFalse([c for c in client.calls if c[0] == "PATCH"], label)

    def test_release_type(self) -> None:
        for public, expected in ((True, "AFTER_APPROVAL"), (False, "MANUAL")):
            client = SelectionClient(build=build_row(), included=[], attached=None)
            quiet(asc_release.set_release_type, client, "V1", public_release=public)
            self.assertEqual(client.calls[-1][2]["data"]["attributes"]["releaseType"], expected)


class TokenRefreshTests(unittest.TestCase):
    def test_token_is_reminted_during_long_waits(self) -> None:
        minted: list[str] = []

        def factory() -> str:
            minted.append(f"t{len(minted)}")
            return minted[-1]

        with mock.patch.object(asc_release.time, "monotonic", side_effect=[0, 100, 700, 700, 800]):
            client = asc_release.AscClient(token_factory=factory)
            self.assertEqual(client._authorization(), "Bearer t0")
            self.assertEqual(client._authorization(), "Bearer t1")
            self.assertEqual(client._authorization(), "Bearer t1")

    def test_client_needs_a_token_source(self) -> None:
        with self.assertRaises(ValueError):
            asc_release.AscClient()


def png(path: Path, width: int, height: int) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                     + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")
    return path


class ScreenshotPlanTests(unittest.TestCase):
    def sets(self, folder: Path) -> tuple:
        return (("APP_IPHONE_67", folder, frozenset({(2796, 1290)})),)

    @repo_content
    def test_repo_marketing_images_are_app_store_ready(self) -> None:
        plan = asc_release.screenshot_plan()
        self.assertEqual([display for display, _ in plan], ["APP_IPHONE_67", "APP_IPAD_PRO_3GEN_129"])
        for display, files in plan:
            self.assertGreaterEqual(len(files), 1, display)
            self.assertLessEqual(len(files), asc_release.APP_SCREENSHOT_SET_MAX, display)
            self.assertEqual(files, sorted(files), "uploads keep file-name order")

    def test_missing_or_empty_folder_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for folder in (Path(tmp) / "missing", Path(tmp)):
                with self.subTest(folder=folder.name), self.assertRaises(SystemExit) as ctx:
                    err = io.StringIO()
                    with redirect_stderr(err):
                        asc_release.screenshot_plan(self.sets(folder))
                self.assertIn("IOS_UPLOAD_SCREENSHOTS", err.getvalue())

    def test_wrong_size_or_non_png_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png(Path(tmp) / "01.png", 2796, 1290)
            png(Path(tmp) / "02.png", 1179, 2556)
            with self.assertRaises(SystemExit):
                quiet(asc_release.screenshot_plan, self.sets(Path(tmp)))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "01.png").write_bytes(b"GIF89a not a png at all......")
            with self.assertRaises(SystemExit):
                quiet(asc_release.screenshot_plan, self.sets(Path(tmp)))

    def test_too_many_files_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(asc_release.APP_SCREENSHOT_SET_MAX + 1):
                png(Path(tmp) / f"{i:02}.png", 2796, 1290)
            with self.assertRaises(SystemExit):
                quiet(asc_release.screenshot_plan, self.sets(Path(tmp)))

    def test_accepted_sizes_pass_in_name_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png(Path(tmp) / "02-b.png", 2796, 1290)
            png(Path(tmp) / "01-a.png", 2796, 1290)
            plan = asc_release.screenshot_plan(self.sets(Path(tmp)))
            self.assertEqual([p.name for p in plan[0][1]], ["01-a.png", "02-b.png"])


class ScreenshotUploadClient:
    """Screenshot sets with a capacity, asset uploads and processing states.

    `delivered` is the assetDeliveryState of each screenshot already in the set
    (COMPLETE unless given; None leaves the state out). `states` are what the
    run's wait reads from GET /appScreenshots/{id}, in order (the last repeats).
    Like App Store Connect, a commit stores the checksum at once, before the
    image is processed.
    """

    def __init__(self, existing: int, *, states: list[str] | None = None, checksums: list[str] | None = None,
                 delivered: list[str | None] | None = None) -> None:
        self.shots = [f"OLD{i}" for i in range(existing)]
        self.checksums = dict(zip(self.shots, checksums or []))
        self.delivery: dict[str, str | None] = dict(
            zip(self.shots, delivered if delivered is not None else ["COMPLETE"] * existing))
        self.states = states or ["COMPLETE"]
        self.calls: list[tuple[str, str]] = []
        self.commits: list[dict] = []
        self.uploads = 0

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def listed(self, shot_id: str) -> dict:
        attributes: dict = {"fileName": f"{shot_id}.png", "sourceFileChecksum": self.checksums.get(shot_id)}
        if self.delivery.get(shot_id) is not None:
            attributes["assetDeliveryState"] = {"state": self.delivery[shot_id], "errors": []}
        return {"id": shot_id, "attributes": attributes}

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if "/appStoreVersionLocalizations" in path and path.startswith("/appStoreVersions/"):
            return {"data": [{"id": "LOC", "attributes": {"locale": "en-US"}}]}
        if path.endswith("/appScreenshotSets") or "/appScreenshotSets?" in path:
            return {"data": [{"id": "SET", "attributes": {"screenshotDisplayType": "APP_IPHONE_67"}}]}
        if "/appScreenshotSets/SET/appScreenshots" in path:
            return {"data": [self.listed(s) for s in self.shots]}
        if path.startswith("/appScreenshots/"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            self.delivery[path.rsplit("/", 1)[1]] = state
            return {"data": {"attributes": {"fileName": "x.png", "assetDeliveryState": {
                "state": state, "errors": [{"description": "bad image"}] if state == "FAILED" else []}}}}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        assert path == "/appScreenshots"
        assert len(self.shots) < asc_release.APP_SCREENSHOT_SET_MAX, "reservation while the set is full"
        new_id = f"NEW{len(self.shots)}-{self.uploads}"
        self.uploads += 1
        self.shots.append(new_id)
        self.delivery[new_id] = "AWAITING_UPLOAD"
        return {"data": {"id": new_id, "attributes": {"uploadOperations": [
            {"url": "https://upload.example/x", "offset": 0, "length": body["data"]["attributes"]["fileSize"],
             "method": "PUT", "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}]}}}

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path))
        attributes = body["data"]["attributes"]
        self.commits.append(attributes)
        shot_id = path.rsplit("/", 1)[1]
        self.checksums[shot_id] = attributes.get("sourceFileChecksum")
        self.delivery[shot_id] = "UPLOAD_COMPLETE"
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path))
        if method == "DELETE":
            self.shots.remove(path.rsplit("/", 1)[1])

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.calls.append(("UPLOAD", url))


class ScreenshotUploadTests(unittest.TestCase):
    def plan(self, tmp: str, count: int) -> list:
        # Real RGB PNGs (the upload path refuses anything else), each with its own checksum.
        files = []
        for i in range(count):
            path = Path(tmp) / f"{i:02}.png"
            path.write_bytes(marked_png(4, 4, str(i)))
            files.append(path)
        return [("APP_IPHONE_67", files)]

    def test_replaces_a_full_set_without_exceeding_capacity(self) -> None:
        client = ScreenshotUploadClient(existing=10)
        with tempfile.TemporaryDirectory() as tmp:
            quiet(asc_release.upload_screenshots, client, version_id="V1", locale="en-US",
                  plan=self.plan(tmp, 5), wait=lambda c, ids: None)
        self.assertEqual(len(client.shots), 5)
        self.assertTrue(all(s.startswith("NEW") for s in client.shots), client.shots)
        self.assertTrue(all(c["uploaded"] is True and len(c["sourceFileChecksum"]) == 32 for c in client.commits))

    def test_a_set_that_already_matches_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(tmp, 3)
            same = [asc_release.md5_hex(p) for p in plan[0][1]]
            for editable in (True, False):
                client = ScreenshotUploadClient(existing=3, checksums=same)
                differing = quiet(asc_release.upload_screenshots, client, version_id="V1", locale="en-US",
                                  plan=plan, wait=lambda c, ids: None, editable=editable, state="READY_FOR_REVIEW")
                self.assertEqual((differing, client.writes()), ([], []), editable)
            # Same images in another order still differ.
            client = ScreenshotUploadClient(existing=3, checksums=list(reversed(same)))
            self.assertEqual(quiet(asc_release.upload_screenshots, client, version_id="V1", locale="en-US",
                                   plan=plan, wait=lambda c, ids: None), ["APP_IPHONE_67"])

    def test_locked_or_read_only_runs_never_change_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(tmp, 2)
            locked = ScreenshotUploadClient(existing=2, checksums=["old", "old"])
            err = io.StringIO()
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(err):
                asc_release.upload_screenshots(locked, version_id="V1", locale="en-US", plan=plan,
                                               wait=lambda c, ids: None, editable=False, state="READY_FOR_REVIEW")
            self.assertIn("READY_FOR_REVIEW", err.getvalue())
            self.assertEqual(locked.writes(), [])
            preview = ScreenshotUploadClient(existing=2)
            differing = quiet(asc_release.upload_screenshots, preview, version_id="V1", locale="en-US", plan=plan,
                              wait=lambda c, ids: None, write=False)
            self.assertEqual((differing, preview.writes()), (["APP_IPHONE_67"], []))

    def test_waits_for_processing_and_fails_on_rejection(self) -> None:
        clock = FakeClock(0)
        client = ScreenshotUploadClient(existing=0, states=["UPLOAD_COMPLETE", "COMPLETE"])
        quiet(asc_release.wait_for_screenshots, client, ["S1"], timeout=60, poll_seconds=5,
              now=clock.now, sleep=clock.sleep)
        self.assertEqual(clock.sleeps, [5])
        failed = ScreenshotUploadClient(existing=0, states=["FAILED"])
        with self.assertRaises(SystemExit):
            quiet(asc_release.wait_for_screenshots, failed, ["S1"], timeout=60, poll_seconds=5,
                  now=clock.now, sleep=clock.sleep)
        stuck = ScreenshotUploadClient(existing=0, states=["UPLOAD_COMPLETE"])
        clock = FakeClock(0)
        with self.assertRaises(SystemExit):
            quiet(asc_release.wait_for_screenshots, stuck, ["S1"], timeout=20, poll_seconds=5,
                  now=clock.now, sleep=clock.sleep)
        self.assertEqual(clock.time, 20)

    def test_the_repo_images_count_as_uploaded_only_once_apple_has_processed_them(self) -> None:
        # The commit stores sourceFileChecksum before Apple processes the image, so a set
        # left by a failed or timed-out wait already holds the repo's checksums.
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(tmp, 3)
            same = [asc_release.md5_hex(p) for p in plan[0][1]]
            for unfinished in ("FAILED", "UPLOAD_COMPLETE", "AWAITING_UPLOAD", None):
                delivered = ["COMPLETE", unfinished, "COMPLETE"]
                with self.subTest(state=unfinished):
                    # Editable: the whole set is uploaded again, and the new images are waited for.
                    client = ScreenshotUploadClient(existing=3, checksums=same, delivered=delivered)
                    waited: list[str] = []
                    differing = quiet(asc_release.upload_screenshots, client, version_id="V1", locale="en-US",
                                      plan=plan, wait=lambda c, ids: waited.extend(ids))
                    self.assertEqual(differing, ["APP_IPHONE_67"])
                    self.assertEqual(waited, client.shots)
                    self.assertFalse({"OLD0", "OLD1", "OLD2"} & set(client.shots), client.shots)
                    # Locked: fails without a write and names the unfinished image.
                    locked = ScreenshotUploadClient(existing=3, checksums=same, delivered=delivered)
                    err = io.StringIO()
                    with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(err):
                        asc_release.upload_screenshots(locked, version_id="V1", locale="en-US", plan=plan,
                                                       wait=lambda c, ids: None, editable=False, state="IN_REVIEW")
                    self.assertIn(f"OLD1.png: {unfinished or 'no processing state'}", err.getvalue())
                    self.assertIn("screenshots cannot change while the version is IN_REVIEW", err.getvalue())
                    self.assertEqual(locked.writes(), [])
                    # Preflight: reported, nothing sent.
                    preview = ScreenshotUploadClient(existing=3, checksums=same, delivered=delivered)
                    out = io.StringIO()
                    with redirect_stdout(out), redirect_stderr(io.StringIO()):
                        differing = asc_release.upload_screenshots(preview, version_id="V1", locale="en-US",
                                                                   plan=plan, wait=lambda c, ids: None, write=False)
                    self.assertEqual((differing, preview.writes()), (["APP_IPHONE_67"], []))
                    self.assertIn("would replace", out.getvalue())

    def test_a_rerun_repairs_a_set_left_by_a_failed_or_timed_out_wait(self) -> None:
        # Apple rejects an image, or is still processing it at the deadline; then
        # "Re-run failed jobs" must upload the set again, not take it as done.
        for outcome in ("FAILED", "UPLOAD_COMPLETE"):
            with self.subTest(first_attempt=outcome), tempfile.TemporaryDirectory() as tmp:
                plan = self.plan(tmp, 2)
                clock = FakeClock(0)

                def run(client: ScreenshotUploadClient) -> list[str]:
                    return quiet(asc_release.upload_screenshots, client, version_id="V1", locale="en-US", plan=plan,
                                 wait=lambda c, ids: asc_release.wait_for_screenshots(
                                     c, ids, timeout=20, poll_seconds=5, now=clock.now, sleep=clock.sleep))

                client = ScreenshotUploadClient(existing=2, states=[outcome])
                with self.assertRaises(SystemExit):
                    run(client)
                first = list(client.shots)
                self.assertEqual([client.checksums[s] for s in first], [asc_release.md5_hex(p) for p in plan[0][1]])
                client.states = ["COMPLETE"]  # this time Apple processes the images
                self.assertEqual(run(client), ["APP_IPHONE_67"])
                self.assertTrue(set(first).isdisjoint(client.shots), client.shots)
                self.assertEqual([client.delivery[s] for s in client.shots], ["COMPLETE", "COMPLETE"])
                # Once the set is done, a further re-run sends nothing.
                writes = len(client.writes())
                self.assertEqual(run(client), [])
                self.assertEqual(len(client.writes()), writes)


def metadata_tree(tmp: str, files: dict[str, str], locale: str = "en-US") -> Path:
    loc = Path(tmp) / "ios" / locale
    loc.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (loc / name).write_text(text)
    return Path(tmp)


class ListingTests(unittest.TestCase):
    @repo_content
    def test_repo_metadata_is_valid_for_every_switch_but_whats_new(self) -> None:
        locales, _ = asc_release.validate_local_inputs(
            upload_listing=True, upload_whats_new=False, upload_screenshots=False,
            metadata_dir=asc_release.METADATA_DIR)
        self.assertIn(asc_release.PRIMARY_LOCALE, locales)

    def test_limits_and_urls_are_enforced(self) -> None:
        cases = {
            "keywords": {"description.txt": "D", "keywords.txt": "k" * 101},
            "description": {"description.txt": "D" * 4001, "keywords.txt": "k"},
            "promo": {"description.txt": "D", "keywords.txt": "k", "promotional_text.txt": "p" * 171},
            "url": {"description.txt": "D", "keywords.txt": "k", "support_url.txt": "http://insecure.example"},
            "name": {"description.txt": "D", "keywords.txt": "k", "name.txt": "n" * 31},
            "missing description": {"keywords.txt": "k"},
        }
        for label, files in cases.items():
            with tempfile.TemporaryDirectory() as tmp, self.subTest(label), self.assertRaises(SystemExit):
                quiet(asc_release.validate_local_inputs, upload_listing=True, upload_whats_new=False,
                      upload_screenshots=False, metadata_dir=metadata_tree(tmp, files))

    def test_whats_new_needs_text_in_every_locale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = metadata_tree(tmp, {"whats_new.txt": "Fixes"})
            metadata_tree(tmp, {"description.txt": "D"}, locale="de-DE")
            with self.assertRaises(SystemExit):
                quiet(asc_release.validate_local_inputs, upload_listing=False, upload_whats_new=True,
                      upload_screenshots=False, metadata_dir=meta)
            (meta / "ios" / "de-DE" / "whats_new.txt").write_text("Fehlerbehebungen")
            locales, _ = asc_release.validate_local_inputs(
                upload_listing=False, upload_whats_new=True, upload_screenshots=False, metadata_dir=meta)
            self.assertEqual(locales, ["de-DE", "en-US"])

    def listing_client(self, version_attrs: dict, info_attrs: dict | None = None, *, editable: bool = True) -> mock.Mock:
        client = mock.Mock()

        def get(path: str) -> dict:
            if path.startswith("/appStoreVersions/V1/appStoreVersionLocalizations"):
                return {"data": [{"id": "LOC", "attributes": {"locale": "en-US", **version_attrs}}]}
            if path.startswith(f"/apps/{APP}/appInfos"):
                state = "PREPARE_FOR_SUBMISSION" if editable else "READY_FOR_SALE"
                return {"data": [{"id": "INFO", "attributes": {"appStoreState": state}}]}
            if path.startswith("/appInfos/INFO/appInfoLocalizations"):
                return {"data": [{"id": "ILOC", "attributes": {"locale": "en-US", **(info_attrs or {})}}]}
            raise AssertionError(path)

        client.get.side_effect = get
        client.patch.return_value = {"data": {}}
        return client

    def upload(self, client, files: dict, *, listing=True, whats_new=False) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            quiet(asc_release.upload_listing, client, app_id=APP, version_id="V1", locale="en-US",
                  metadata_dir=metadata_tree(tmp, files), include_listing=listing, include_whats_new=whats_new)

    def test_only_changed_fields_are_sent_and_whats_new_has_its_own_switch(self) -> None:
        files = {"description.txt": "New desc\n", "keywords.txt": "same", "whats_new.txt": "Fixes"}
        client = self.listing_client({"description": "Old desc", "keywords": "same"})
        self.upload(client, files)
        version_patch = client.patch.call_args_list[0]
        self.assertEqual(version_patch.args[0], "/appStoreVersionLocalizations/LOC")
        self.assertEqual(version_patch.args[1]["data"]["attributes"], {"description": "New desc"})
        client = self.listing_client({"description": "New desc", "keywords": "same"})
        self.upload(client, files, listing=False, whats_new=True)
        self.assertEqual(client.patch.call_args.args[1]["data"]["attributes"], {"whatsNew": "Fixes"})
        client = self.listing_client({"description": "New desc", "keywords": "same", "whatsNew": "Fixes"})
        self.upload(client, files, listing=True, whats_new=True)
        client.patch.assert_not_called()

    def test_app_info_is_updated_only_while_editable(self) -> None:
        files = {"description.txt": "D", "keywords.txt": "k", "subtitle.txt": "A new subtitle"}
        client = self.listing_client({"description": "D", "keywords": "k"}, {"subtitle": "Old"})
        self.upload(client, files)
        self.assertEqual(client.patch.call_args.args[0], "/appInfoLocalizations/ILOC")
        self.assertEqual(client.patch.call_args.args[1]["data"]["attributes"], {"subtitle": "A new subtitle"})
        locked = self.listing_client({"description": "D", "keywords": "k"}, {"subtitle": "Old"}, editable=False)
        with self.assertRaises(SystemExit):
            self.upload(locked, files)
        same = self.listing_client({"description": "D", "keywords": "k"}, {"subtitle": "A new subtitle"}, editable=False)
        self.upload(same, files)
        same.patch.assert_not_called()

    def test_missing_localization_fails(self) -> None:
        client = mock.Mock()
        client.get.return_value = {"data": [{"id": "LOC", "attributes": {"locale": "fr-FR"}}]}
        with self.assertRaises(SystemExit):
            self.upload(client, {"description.txt": "D"})

    def sync(self, client, files: dict, **kwargs) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return quiet(asc_release.upload_listing, client, app_id=APP, version_id="V1", locale="en-US",
                         metadata_dir=metadata_tree(tmp, files), **kwargs)

    def test_locked_text_is_compared_never_changed(self) -> None:
        # WAITING_FOR_REVIEW / IN_REVIEW: a re-run passes only when nothing would change.
        files = {"description.txt": "D", "keywords.txt": "k", "whats_new.txt": "Fixes", "subtitle.txt": "Sub"}
        locked = {"text_editable": False, "include_whats_new": True, "state": "WAITING_FOR_REVIEW"}
        same = self.listing_client({"description": "D", "keywords": "k", "whatsNew": "Fixes"}, {"subtitle": "Sub"},
                                   editable=False)
        self.assertEqual(self.sync(same, files, **locked), [])
        same.patch.assert_not_called()
        for version_attrs, info_attrs in (({"description": "Old", "keywords": "k", "whatsNew": "Fixes"}, {"subtitle": "Sub"}),
                                          ({"description": "D", "keywords": "k", "whatsNew": "Fixes"}, {"subtitle": "Old"})):
            differs = self.listing_client(version_attrs, info_attrs, editable=False)
            with self.subTest(version=version_attrs, info=info_attrs), self.assertRaises(SystemExit):
                self.sync(differs, files, **locked)
            differs.patch.assert_not_called()

    def test_read_only_run_reports_changes_without_sending_them(self) -> None:
        files = {"description.txt": "New", "keywords.txt": "k", "subtitle.txt": "New sub"}
        client = self.listing_client({"description": "Old", "keywords": "k"}, {"subtitle": "Old"})
        self.assertEqual(self.sync(client, files, write=False), ["en-US description", "en-US subtitle"])
        client.patch.assert_not_called()

    def test_app_info_of_the_version_in_review_is_compared_not_the_live_one(self) -> None:
        client = mock.Mock()
        locs = {"LIVE": "Live subtitle", "NEXT": "Next subtitle"}

        def get(path: str) -> dict:
            if path.startswith("/appStoreVersions/V1/appStoreVersionLocalizations"):
                return {"data": [{"id": "LOC", "attributes": {"locale": "en-US", "description": "D", "keywords": "k"}}]}
            if path.startswith(f"/apps/{APP}/appInfos"):
                return {"data": [{"id": "LIVE", "attributes": {"appStoreState": "READY_FOR_SALE"}},
                                 {"id": "NEXT", "attributes": {"appStoreState": "WAITING_FOR_REVIEW"}}]}
            for info_id, subtitle in locs.items():
                if path.startswith(f"/appInfos/{info_id}/appInfoLocalizations"):
                    return {"data": [{"id": f"L-{info_id}", "attributes": {"locale": "en-US", "subtitle": subtitle}}]}
            raise AssertionError(path)

        client.get.side_effect = get
        self.assertEqual(asc_release.choose_app_info(client, APP)[0]["id"], "NEXT")
        files = {"description.txt": "D", "keywords.txt": "k", "subtitle.txt": "Next subtitle"}
        self.assertEqual(self.sync(client, files, text_editable=False, state="WAITING_FOR_REVIEW"), [])
        client.patch.assert_not_called()

    def test_ready_for_review_text_and_app_info_stay_editable(self) -> None:
        self.assertIn("READY_FOR_REVIEW", asc_release.TEXT_EDITABLE_VERSION_STATES)
        self.assertIn("READY_FOR_REVIEW", asc_release.EDITABLE_APP_INFO_STATES)
        self.assertNotIn("READY_FOR_REVIEW", asc_release.EDITABLE_VERSION_STATES)  # screenshots are locked


class ProductClient:
    """Review submissions, subscription/IAP versions and product states for product attachment."""

    def __init__(self, collections: dict[str, list[dict]]) -> None:
        self.collections = collections
        self.posts: list[tuple[str, dict]] = []
        self.requests: list[tuple[str, str, dict | None]] = []

    def get(self, path: str) -> dict:
        return {"data": self.collections.get(path, [])}

    def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        return {"data": {"id": "NEW"}}

    def patch(self, path: str, body: dict) -> dict:
        raise AssertionError(f"unexpected PATCH {path}")

    def request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path, body))
        return None


UNLOCK = "com.apoorvdarshan.Abysm.unlockdeep"
ITEMS = asc_release.draft_items_path("SUB1")


def product_collections(unlock_state: str = "READY_TO_SUBMIT", *, draft: bool = True) -> dict[str, list[dict]]:
    return {
        asc_release.draft_submission_query(APP): [{"id": "SUB1"}] if draft else [],
        f"/apps/{APP}/subscriptionGroups?limit=200": [{"id": "G1"}],
        "/subscriptionGroups/G1/subscriptions?limit=200": [
            {"id": "S1", "attributes": {"name": "Deep Pass", "productId": "com.apoorvdarshan.Abysm.pass",
                                        "state": "DEVELOPER_REJECTED"}},
            {"id": "SX", "attributes": {"name": "Legacy", "productId": "com.apoorvdarshan.Abysm.legacy",
                                        "state": "READY_TO_SUBMIT"}},
        ],
        "/subscriptions/S1/versions": [
            {"id": "V1b", "attributes": {"version": 2}},
            {"id": "V1a", "attributes": {"version": 1}},
        ],
        f"/apps/{APP}/inAppPurchasesV2?limit=200": [
            {"id": "IAP1", "attributes": {"name": "Unlock the Deep", "productId": UNLOCK, "state": unlock_state}},
            {"id": "IAP2", "attributes": {"name": "Other", "productId": "com.apoorvdarshan.Abysm.other",
                                          "state": "READY_TO_SUBMIT"}},
        ],
        # In-app purchase versions are a v2 resource (absolute URL).
        asc_release.iap_versions_path("IAP1"): [
            {"id": "IV1", "attributes": {"version": 1, "state": "APPROVED"}},
            {"id": "IV2", "attributes": {"version": 2, "state": "PREPARE_FOR_SUBMISSION"}},
        ],
    }


def item_targets(posts: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """(relationship, id) of every review submission item posted, besides the submission itself."""
    targets = []
    for path, body in posts:
        if path == "/reviewSubmissionItems":
            for name, rel in body["data"]["relationships"].items():
                if name != "reviewSubmission":
                    targets.append((name, rel["data"]["id"]))
    return targets


class ProductAttachTests(unittest.TestCase):
    INTENDED = {UNLOCK, "com.apoorvdarshan.Abysm.pass"}

    def test_ready_catalog_products_join_the_draft_and_nothing_is_submitted(self) -> None:
        client = ProductClient(product_collections())
        result = quiet(asc_release.attach_catalog_products, client, APP, intended_ids=self.INTENDED)
        self.assertEqual((result.attached, result.added_items, result.submission_id), (2, True, "SUB1"))
        # The newest version of each product becomes an item of the same draft as the app.
        self.assertEqual(item_targets(client.posts), [("subscriptionVersion", "V1b"), ("inAppPurchaseVersion", "IV2")])
        iap_item = [b for p, b in client.posts if p == "/reviewSubmissionItems"][1]["data"]["relationships"]
        self.assertEqual(iap_item["inAppPurchaseVersion"]["data"]["type"], "inAppPurchaseVersions")
        self.assertEqual(iap_item["reviewSubmission"]["data"]["id"], "SUB1")
        # Never the deprecated standalone endpoint, never a submit.
        self.assertNotIn("/inAppPurchaseSubmissions", [p for p, _ in client.posts])
        self.assertEqual(client.requests, [])

    def test_approved_unlock_is_never_resubmitted(self) -> None:
        client = ProductClient(product_collections(unlock_state="APPROVED"))
        result = quiet(asc_release.attach_catalog_products, client, APP, intended_ids={UNLOCK})
        self.assertEqual(result.attached, 0)
        self.assertEqual(client.posts, [])

    def test_retry_skips_products_already_in_the_draft(self) -> None:
        collections = product_collections()
        collections[ITEMS] = [
            {"id": "I1", "relationships": {"subscriptionVersion": {"data": {"id": "V1b"}}}},
            {"id": "I2", "relationships": {"inAppPurchaseVersion": {"data": {"id": "IV2"}}}},
        ]
        client = ProductClient(collections)
        result = quiet(asc_release.attach_catalog_products, client, APP, intended_ids=self.INTENDED)
        self.assertEqual((result.attached, result.added_items), (2, False))
        self.assertEqual(client.posts, [])

    def test_draft_items_include_in_app_purchase_versions(self) -> None:
        self.assertIn("inAppPurchaseVersion", asc_release.draft_items_path("SUB1"))

    def test_failed_or_versionless_required_product_raises(self) -> None:
        client = ProductClient(product_collections())
        client.post = mock.Mock(side_effect=asc_release.AscHttpError(409, "conflict"))
        with self.assertRaises(asc_release.ProductSubmitError):
            quiet(asc_release.attach_catalog_products, client, APP, intended_ids={UNLOCK})
        collections = product_collections()
        collections[asc_release.iap_versions_path("IAP1")] = []
        client = ProductClient(collections)
        with self.assertRaisesRegex(asc_release.ProductSubmitError, "no version to submit"):
            quiet(asc_release.attach_catalog_products, client, APP, intended_ids={UNLOCK})
        self.assertEqual(client.posts, [])

    def test_a_draft_is_created_only_when_something_needs_one(self) -> None:
        idle = ProductClient(product_collections(unlock_state="APPROVED", draft=False))
        result = quiet(asc_release.attach_catalog_products, idle, APP, intended_ids={UNLOCK})
        self.assertEqual((result.attached, result.submission_id, idle.posts), (0, "", []))
        busy = ProductClient(product_collections(draft=False))
        result = quiet(asc_release.attach_catalog_products, busy, APP, intended_ids={UNLOCK})
        self.assertEqual([p for p, _ in busy.posts], ["/reviewSubmissions", "/reviewSubmissionItems"])
        self.assertEqual(result.submission_id, "NEW")

    def test_empty_catalog_submits_nothing(self) -> None:
        client = ProductClient(product_collections())
        result = quiet(asc_release.attach_catalog_products, client, APP, intended_ids=set())
        self.assertEqual(result.attached, 0)
        self.assertEqual(client.posts, [])

    def test_submit_for_review_does_not_add_the_version_twice(self) -> None:
        collections = {ITEMS: [{"id": "I1", "relationships": {"appStoreVersion": {"data": {"id": "AV1"}}}}]}
        client = ProductClient(collections)
        quiet(asc_release.submit_for_review, client, APP, "AV1", "PREPARE_FOR_SUBMISSION", submission_id="SUB1")
        self.assertEqual([p for p, _ in client.posts], [])
        self.assertIn(("PATCH", "/reviewSubmissions/SUB1"), [(m, p) for m, p, _ in client.requests])
        fresh = ProductClient({})
        quiet(asc_release.submit_for_review, fresh, APP, "AV1", "PREPARE_FOR_SUBMISSION", submission_id="SUB1")
        self.assertEqual([p for p, _ in fresh.posts], ["/reviewSubmissionItems"])

    def test_catalog_products_are_intended(self) -> None:
        self.assertEqual(asc_release.intended_product_ids(), {UNLOCK})


class CatalogTests(unittest.TestCase):
    def inputs(self) -> tuple[dict, dict, str]:
        return (
            json.loads((ROOT / "store" / "catalog" / "products.json").read_text()),
            json.loads((ROOT / "Abysm.storekit").read_text()),
            (ROOT / "Abysm" / "StoreManager.swift").read_text(),
        )

    def test_repo_catalog_matches_the_app(self) -> None:
        self.assertEqual(validate_catalog.validate(*self.inputs()), [UNLOCK])

    def test_mismatches_are_rejected(self) -> None:
        catalog, storekit, swift = self.inputs()
        broken = {
            "storekit": (catalog, {**storekit, "products": []}, swift),
            "entitlement": ({**catalog, "nonConsumables": [{"id": UNLOCK, "entitlement": "other"}]}, storekit, swift),
            "duplicate": ({**catalog, "consumables": [{"id": UNLOCK}]}, storekit, swift),
            "foreign id": ({**catalog, "nonConsumables": [{"id": "com.other.unlock"}]}, storekit, swift),
            "type": ({**catalog, "nonConsumables": [], "consumables": [{"id": UNLOCK}]}, storekit, swift),
            "product": (catalog, storekit, swift.replace(UNLOCK, "com.apoorvdarshan.Abysm.other")),
        }
        for label, args in broken.items():
            with self.subTest(label), self.assertRaises(validate_catalog.CatalogError):
                validate_catalog.validate(*args)


class AscCliTests(unittest.TestCase):
    def run_cli(self, env: dict[str, str], *args: str, dry_run: bool = True) -> tuple[int, str]:
        out = io.StringIO()
        full_env = {k: v for k, v in os.environ.items() if k not in ASC_FLAGS}
        full_env.update(env)
        argv = ["asc_release.py", *(["--dry-run"] if dry_run else []), *args]
        with mock.patch.dict(os.environ, full_env, clear=True), mock.patch.object(sys, "argv", argv), \
                redirect_stdout(out), redirect_stderr(out):
            try:
                asc_release.main()
            except SystemExit as exc:
                return (exc.code if isinstance(exc.code, int) else 1), out.getvalue()
        return 0, out.getvalue()

    def test_everything_off_is_a_no_op(self) -> None:
        code, output = self.run_cli({}, dry_run=False)
        self.assertEqual(code, 0)
        self.assertIn("skipped", output)

    def test_submit_without_this_runs_build_fails(self) -> None:
        code, output = self.run_cli({"SUBMIT_IOS_REVIEW": "true"})
        self.assertEqual(code, 1)
        self.assertIn("--build-id", output)

    def test_public_release_without_submit_fails(self) -> None:
        code, _ = self.run_cli({"UPLOAD_LISTING": "true", "PUBLIC_RELEASE": "true"})
        self.assertEqual(code, 1)

    def valid_tree(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return str(metadata_tree(tmp.name, {"description.txt": "D", "keywords.txt": "k"}))

    def test_whats_new_without_text_fails(self) -> None:
        # Turning What's New on needs whats_new.txt in every locale first.
        code, output = self.run_cli({"UPLOAD_WHATS_NEW": "true"}, "--metadata-dir", self.valid_tree())
        self.assertEqual(code, 1)
        self.assertIn("whats_new.txt", output)

    @repo_content
    @needs_pillow  # the committed screenshots store alpha; their RGB copies need Pillow
    def test_full_dry_run_with_repo_inputs(self) -> None:
        code, output = self.run_cli(
            {"UPLOAD_LISTING": "true", "UPLOAD_SCREENSHOTS": "true", "SUBMIT_IOS_REVIEW": "true"},
            "--build-id", "DRY-RUN", "--build-number", "3")
        self.assertEqual(code, 0, output)
        self.assertIn("no API calls", output)
        self.assertIn("screenshots=10", output)

    def test_live_run_requires_version_and_credentials(self) -> None:
        tree = self.valid_tree()
        for mode in ((), ("--preflight",)):
            with self.subTest(mode=mode):
                code, output = self.run_cli({"UPLOAD_LISTING": "true"}, "--metadata-dir", tree, *mode, dry_run=False)
                self.assertEqual(code, 1)
                self.assertIn("--version", output)
                code, output = self.run_cli(
                    {"UPLOAD_LISTING": "true", "APP_STORE_CONNECT_API_KEY_ID": "", "APP_STORE_CONNECT_ISSUER_ID": "",
                     "APP_STORE_CONNECT_API_KEY_P8": ""}, "--metadata-dir", tree, "--version", VERSION, *mode,
                    dry_run=False)
                self.assertEqual(code, 1)
                self.assertIn("missing ASC credentials", output)


IPHONE = "APP_IPHONE_67"
IPAD = "APP_IPAD_PRO_3GEN_129"
REPO_TEXT = {"description.txt": "D", "keywords.txt": "k", "whats_new.txt": "Fixes", "subtitle.txt": "Sub"}
LIVE_TEXT = {"description": "D", "keywords": "k", "whatsNew": "Fixes"}
LIVE_INFO = {"subtitle": "Sub"}


class FakeAsc:
    """App Store Connect stand-in for asc_release.main() (live path, no network).

    One app with one App Store version V1 (and its App Info), localizations for
    `locales`, screenshot sets, the build B9 this run uploaded, a draft review
    submission and the "Unlock the Deep" in-app purchase with its versions.
    `other_versions` are the app's other iOS versions; POST /appStoreVersions
    creates V1 in Prepare for Submission.
    """

    def __init__(self, *, state: str = "PREPARE_FOR_SUBMISSION", attached: str | None = "OLD",
                 revert_on_read: int | None = None, version_exists: bool = True, release_type: str | None = None,
                 version_text: dict | None = None, info_text: dict | None = None,
                 screenshots: dict[str, list[str]] | None = None,
                 screenshot_states: dict[str, list[str]] | None = None, unlock_state: str = "APPROVED",
                 unlock_versions: list[dict] | None = None, items: list[dict] | None = None,
                 locales: tuple[str, ...] = ("en-US",), other_versions: list[tuple[str, str]] | None = None,
                 app_id: str = APP) -> None:
        self.state = state
        self.attached = attached
        # The selection reads back as OLD from this read of /appStoreVersions/V1/build on.
        self.revert_on_read = revert_on_read
        self.version_exists = version_exists
        self.release_type = release_type
        self.version_text = dict(LIVE_TEXT if version_text is None else version_text)
        self.info_text = dict(LIVE_INFO if info_text is None else info_text)
        self.screenshots = screenshots or {}
        # assetDeliveryState of each screenshot in a set (COMPLETE unless given).
        self.screenshot_states = screenshot_states or {}
        self.unlock_state = unlock_state
        self.unlock_versions = unlock_versions if unlock_versions is not None else [
            {"id": "IV1", "attributes": {"version": 1, "state": "PREPARE_FOR_SUBMISSION"}}]
        # A READY_FOR_REVIEW version sits in its draft, as in App Store Connect.
        held = [{"id": "ITEM-V1", "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "V1"}}}}]
        self.items = list(items) if items is not None else (held if state == "READY_FOR_REVIEW" else [])
        # Display types of the screenshot sets this run created (they start empty).
        self.created_sets: list[str] = []
        self.locales = locales
        # The app's other iOS versions as (versionString, appStoreState).
        self.other_versions = [("1.0", "READY_FOR_SALE")] if other_versions is None else other_versions
        # The app the bundle id resolves to.
        self.app_id = app_id
        self.build_reads = 0
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, dict | None]] = []

    def info_state(self) -> str:
        return "PREPARE_FOR_SUBMISSION" if self.state in asc_release.EDITABLE_VERSION_STATES else self.state

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if path.startswith("/apps?"):
            return {"data": [{"id": self.app_id, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}]}
        if path.startswith(f"/apps/{APP}/appStoreVersions"):
            rows = [{"id": f"V0-{i}", "attributes": {"versionString": number, "appStoreState": state}}
                    for i, (number, state) in enumerate(self.other_versions)]
            if self.version_exists:
                rows.append({"id": "V1", "attributes": {"versionString": VERSION, "appStoreState": self.state,
                                                        "releaseType": self.release_type}})
            return {"data": rows}
        if path.startswith("/appStoreVersions/V1/appStoreVersionLocalizations"):
            return {"data": [{"id": f"LOC-{loc}", "attributes": {"locale": loc, **self.version_text}}
                             for loc in self.locales]}
        if path.startswith(f"/apps/{APP}/appInfos"):
            return {"data": [{"id": "LIVE", "attributes": {"appStoreState": "READY_FOR_SALE"}},
                             {"id": "INFO", "attributes": {"appStoreState": self.info_state()}}]}
        if path.startswith("/appInfos/"):
            return {"data": [{"id": f"ILOC-{loc}", "attributes": {"locale": loc, **self.info_text}}
                             for loc in self.locales]}
        if path.startswith("/appStoreVersionLocalizations/") and "/appScreenshotSets" in path:
            return {"data": [{"id": f"SET-{kind}", "attributes": {"screenshotDisplayType": kind}}
                             for kind in self.screenshots]}
        if path.startswith("/appScreenshotSets/SET-"):
            kind = path.split("/")[2][len("SET-"):]
            checksums = self.screenshots[kind]
            states = self.screenshot_states.get(kind, ["COMPLETE"] * len(checksums))
            return {"data": [{"id": f"{kind}-{i}", "attributes": {"sourceFileChecksum": checksum,
                                                                 "assetDeliveryState": {"state": state}}}
                             for i, (checksum, state) in enumerate(zip(checksums, states))]}
        if path.startswith("/appScreenshotSets/NEW-"):
            return {"data": []}
        if path.startswith("/appScreenshots/"):
            return {"data": {"attributes": {"fileName": "x.png", "assetDeliveryState": {"state": "COMPLETE"}}}}
        if path.startswith("/builds/B9"):
            return {"data": build_row("B9"), "included": [pre_row()]}
        if path == "/appStoreVersions/V1/build":
            self.build_reads += 1
            if self.revert_on_read is not None and self.build_reads >= self.revert_on_read:
                return {"data": {"type": "builds", "id": "OLD"}}
            return {"data": {"type": "builds", "id": self.attached} if self.attached else None}
        if path.startswith(f"/apps/{APP}/reviewSubmissions"):
            return {"data": [{"id": "SUB1"}]}
        if path.startswith("/reviewSubmissions/SUB1/items"):
            return {"data": self.items}
        if path.startswith(f"/apps/{APP}/subscriptionGroups"):
            return {"data": []}
        if path.startswith(f"/apps/{APP}/inAppPurchasesV2"):
            return {"data": [{"id": "IAP1", "attributes": {"name": "Unlock the Deep", "productId": UNLOCK,
                                                           "state": self.unlock_state}}]}
        if path == asc_release.iap_versions_path("IAP1"):
            return {"data": self.unlock_versions}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        self.bodies.append(("POST", path, body))
        if path == "/reviewSubmissionItems":
            related = {k: v for k, v in body["data"]["relationships"].items() if k != "reviewSubmission"}
            self.items.append({"id": f"ITEM{len(self.items)}", "relationships": related})
        if path == "/appStoreVersions":
            self.version_exists = True
            self.state = "PREPARE_FOR_SUBMISSION"
            return {"data": {"id": "V1"}}
        if path == "/appScreenshotSets":
            display = body["data"]["attributes"]["screenshotDisplayType"]
            self.created_sets.append(display)
            return {"data": {"id": f"NEW-{display}", "attributes": {"screenshotDisplayType": display}}}
        return {"data": {"id": "ITEM"}}

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path))
        self.bodies.append(("PATCH", path, body))
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path))
        self.bodies.append((method, path, body))
        if method == "PATCH" and path == "/appStoreVersions/V1/relationships/build":
            self.attached = body["data"]["id"]
        return None

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.calls.append(("UPLOAD", url))

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


SUBMIT = ("PATCH", "/reviewSubmissions/SUB1")
SELECT = ("PATCH", "/appStoreVersions/V1/relationships/build")


class MainFlow(unittest.TestCase):
    """Runs asc_release.main() against FakeAsc with a temporary listing and screenshots.

    The screenshots are real RGB PNGs, staged without Pillow (copied as they
    are), so the copies the run uploads have the committed files' checksums.
    The optional iPhone Duo and Apple Watch folders start empty.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.metadata = metadata_tree(tmp.name, REPO_TEXT)
        iphone = Path(tmp.name) / "iphone"
        ipad = Path(tmp.name) / "ipad"
        iphone.mkdir()
        ipad.mkdir()
        self.shots = {
            IPHONE: [self.frame(iphone / f"0{i}.png", 2796, 1290) for i in range(2)],
            IPAD: [self.frame(ipad / f"0{i}.png", 2752, 2064) for i in range(2)],
        }
        self.sets = (
            (IPHONE, iphone, frozenset({(2796, 1290)})),
            (IPAD, ipad, frozenset({(2752, 2064)})),
        )
        self.duo = self.tmp / "iphone-duo"
        self.watch = self.tmp / "watch"
        self.optional_sets = tuple((display, self.tmp / folder.name, accepted)
                                   for display, folder, accepted in asc_release.OPTIONAL_SCREENSHOT_SETS)
        self.pillow = None
        self.creative_dir = self.tmp / "creative"

    @staticmethod
    def frame(path: Path, width: int, height: int, colour_type: int = 2) -> Path:
        """A real PNG with its own checksum (the file's path is in a text chunk)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(marked_png(width, height, str(path), colour_type))
        return path

    def repo_checksums(self) -> dict[str, list[str]]:
        return {kind: [asc_release.md5_hex(p) for p in files] for kind, files in self.shots.items()}

    def run_main(self, fake: FakeAsc, switches: dict[str, str], *args: str, build: bool = True) -> tuple[int, str]:
        argv = ["asc_release.py", "--version", VERSION, "--metadata-dir", str(self.metadata),
                "--creative-dir", str(self.creative_dir), *args]
        if build:
            argv += ["--build-id", "B9", "--build-number", "3"]
        env = {k: v for k, v in os.environ.items() if k not in ASC_FLAGS}
        env.update(switches)
        out = io.StringIO()
        code = 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(asc_release, "client_from_environment", return_value=fake), \
                mock.patch.object(asc_release, "SCREENSHOT_SETS", self.sets), \
                mock.patch.object(asc_release, "OPTIONAL_SCREENSHOT_SETS", self.optional_sets), \
                mock.patch.object(asc_release, "WATCH_SCREENSHOTS", self.watch), \
                mock.patch.object(asc_release, "try_import_pillow", return_value=self.pillow), \
                redirect_stdout(out), redirect_stderr(out):
            try:
                asc_release.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()


SUBMIT_ONLY = {"SUBMIT_IOS_REVIEW": "true"}
TEXT_AND_SUBMIT = {"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "SUBMIT_IOS_REVIEW": "true"}
EVERYTHING = {**TEXT_AND_SUBMIT, "UPLOAD_SCREENSHOTS": "true"}


class LiveSubmitFlowTests(MainFlow):
    def test_selects_rechecks_then_submits(self) -> None:
        fake = FakeAsc()
        code, output = self.run_main(fake, SUBMIT_ONLY)
        self.assertEqual(code, 0, output)
        calls = fake.calls
        select = calls.index(SELECT)
        submit = calls.index(SUBMIT)
        reads = [i for i, c in enumerate(calls) if c == ("GET", "/appStoreVersions/V1/build")]
        build_checks = [i for i, c in enumerate(calls) if c[0] == "GET" and c[1].startswith("/builds/B9")]
        self.assertEqual(len(reads), 3, "checked before selecting, read back, and rechecked")
        self.assertTrue(reads[0] < select < reads[1] < reads[2] < submit)
        self.assertEqual(len(build_checks), 2)
        self.assertTrue(build_checks[1] < reads[2] < submit, "build must be re-verified just before submit")
        self.assertIn(("PATCH", "/appStoreVersions/V1"), calls)  # release type
        self.assertEqual(item_targets([(p, b) for m, p, b in fake.bodies if m == "POST"]), [("appStoreVersion", "V1")])

    def test_unlock_goes_out_with_the_version_never_on_its_own(self) -> None:
        fake = FakeAsc(unlock_state="READY_TO_SUBMIT")
        code, output = self.run_main(fake, SUBMIT_ONLY)
        self.assertEqual(code, 0, output)
        posts = [(p, b) for m, p, b in fake.bodies if m == "POST"]
        self.assertEqual(item_targets(posts), [("inAppPurchaseVersion", "IV1"), ("appStoreVersion", "V1")])
        self.assertNotIn(("POST", "/inAppPurchaseSubmissions"), fake.calls)
        self.assertEqual(fake.calls.count(SUBMIT), 1, "one submit sends the version and its products")

    def test_selection_changed_before_submit_aborts_without_any_review_write(self) -> None:
        # Pre-check (1) and read-back (2) see this run's build; the final recheck (3) does not.
        fake = FakeAsc(revert_on_read=3, unlock_state="READY_TO_SUBMIT")
        code, output = self.run_main(fake, SUBMIT_ONLY)
        self.assertEqual(code, 1)
        self.assertIn("not submitting", output)
        self.assertNotIn(SUBMIT, fake.calls)
        self.assertNotIn(("POST", "/inAppPurchaseSubmissions"), fake.calls)
        # Unlock the Deep only joined the unsubmitted draft; nothing reached App Review.
        self.assertEqual(item_targets([(p, b) for m, p, b in fake.bodies if m == "POST"]),
                         [("inAppPurchaseVersion", "IV1")])

    def test_waiting_for_review_with_another_build_aborts(self) -> None:
        fake = FakeAsc(state="WAITING_FOR_REVIEW", attached="OTHER")
        code, _ = self.run_main(fake, SUBMIT_ONLY)
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])

    def test_waiting_for_review_with_this_build_is_idempotent(self) -> None:
        fake = FakeAsc(state="WAITING_FOR_REVIEW", attached="B9")
        code, output = self.run_main(fake, SUBMIT_ONLY)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [])
        self.assertIn("already WAITING_FOR_REVIEW", output)

    def test_live_version_is_not_submitted(self) -> None:
        fake = FakeAsc(state="READY_FOR_SALE")
        code, _ = self.run_main(fake, SUBMIT_ONLY)
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])


class RerunTests(MainFlow):
    """Re-running the store step after a partly finished submission (IOS-REL-3)."""

    def test_ready_for_review_rerun_with_text_switches_submits(self) -> None:
        # The first attempt left the version in the draft with this run's build; its
        # submit PATCH failed. Text still matches the repo, so only the submit is sent.
        fake = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL",
                       items=[{"id": "I1", "relationships": {"appStoreVersion": {"data": {"id": "V1"}}}}])
        code, output = self.run_main(fake, TEXT_AND_SUBMIT)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [SUBMIT])

    def test_ready_for_review_text_can_still_change(self) -> None:
        fake = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL", version_text={**LIVE_TEXT, "whatsNew": "Old"})
        code, output = self.run_main(fake, TEXT_AND_SUBMIT)
        self.assertEqual(code, 0, output)
        self.assertIn(("PATCH", "/appStoreVersionLocalizations/LOC-en-US"), fake.calls)
        self.assertEqual(fake.calls[-1], SUBMIT)

    def test_ready_for_review_screenshots_must_already_match(self) -> None:
        same = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL", screenshots=self.repo_checksums())
        code, output = self.run_main(same, EVERYTHING)
        self.assertEqual(code, 0, output)
        self.assertEqual(same.writes(), [SUBMIT])  # the version is already in the draft
        changed = self.repo_checksums()
        changed[IPAD] = list(reversed(changed[IPAD]))
        differs = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL", screenshots=changed)
        code, output = self.run_main(differs, EVERYTHING)
        self.assertEqual(code, 1)
        self.assertIn("screenshots cannot change while the version is READY_FOR_REVIEW", output)
        self.assertEqual(differs.writes(), [])
        # The repo's checksums are not enough: Apple must have processed every image.
        unprocessed = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL",
                              screenshots=self.repo_checksums(),
                              screenshot_states={IPHONE: ["UPLOAD_COMPLETE", "COMPLETE"]})
        code, output = self.run_main(unprocessed, EVERYTHING)
        self.assertEqual(code, 1, output)
        self.assertIn("UPLOAD_COMPLETE", output)
        self.assertEqual(unprocessed.writes(), [])

    def test_submitted_rerun_passes_only_when_nothing_would_change(self) -> None:
        for state in sorted(asc_release.SUBMITTED_VERSION_STATES):
            with self.subTest(state=state):
                same = FakeAsc(state=state, attached="B9", screenshots=self.repo_checksums())
                code, output = self.run_main(same, EVERYTHING)
                self.assertEqual(code, 0, output)
                self.assertEqual(same.writes(), [])
                for fake in (FakeAsc(state=state, attached="B9", screenshots=self.repo_checksums(),
                                     version_text={**LIVE_TEXT, "whatsNew": "Old"}),
                             FakeAsc(state=state, attached="B9", screenshots=self.repo_checksums(),
                                     info_text={"subtitle": "Old"}),
                             FakeAsc(state=state, attached="B9", screenshots={}),
                             FakeAsc(state=state, attached="B9", screenshots=self.repo_checksums(),
                                     screenshot_states={IPAD: ["COMPLETE", "FAILED"]}),
                             FakeAsc(state=state, attached="B9", screenshots=self.repo_checksums(),
                                     unlock_state="READY_TO_SUBMIT"),
                             FakeAsc(state=state, attached="OTHER", screenshots=self.repo_checksums())):
                    code, output = self.run_main(fake, EVERYTHING)
                    self.assertEqual(code, 1, output)
                    self.assertEqual(fake.writes(), [])

    def test_submitted_rerun_never_sends_a_product_to_review_on_its_own(self) -> None:
        # Unlock the Deep is Ready to Submit while the version is already in review, so
        # it did not go out with the version. The re-run names it and writes nothing:
        # no draft, no review item and no product-only submission.
        for state in sorted(asc_release.SUBMITTED_VERSION_STATES):
            with self.subTest(state=state):
                fake = FakeAsc(state=state, attached="B9", unlock_state="READY_TO_SUBMIT")
                code, output = self.run_main(fake, SUBMIT_ONLY)
                self.assertEqual(code, 1, output)
                self.assertEqual(fake.writes(), [])
                self.assertIn(f"in-app purchase Unlock the Deep ({UNLOCK})", output)
                self.assertIn("never sent to App Review without the version", output)

    def test_an_identical_rerun_in_an_editable_state_sends_nothing_but_the_submit(self) -> None:
        fake = FakeAsc(attached="B9", release_type="MANUAL", screenshots=self.repo_checksums())
        code, output = self.run_main(fake, EVERYTHING)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [("POST", "/reviewSubmissionItems"), SUBMIT])


class PreflightTests(MainFlow):
    """`asc_release.py --preflight`: read-only, before anything is built (IOS-REL-2)."""

    def preflight(self, fake: FakeAsc, switches: dict[str, str]) -> tuple[int, str]:
        code, output = self.run_main(fake, switches, "--preflight", build=False)
        self.assertEqual(fake.writes(), [], "the preflight must never change App Store Connect")
        return code, output

    def test_missing_version_fails_with_the_fix(self) -> None:
        code, output = self.preflight(FakeAsc(version_exists=False), {"UPLOAD_LISTING": "true"})
        self.assertEqual(code, 1)
        self.assertIn("turn on IOS_CREATE_VERSION, or create the version in App Store Connect first", output)

    def test_ready_version_passes_and_reports_what_would_change(self) -> None:
        fake = FakeAsc(version_text={**LIVE_TEXT, "description": "Old"}, unlock_state="READY_TO_SUBMIT")
        code, output = self.preflight(fake, EVERYTHING)
        self.assertEqual(code, 0, output)
        self.assertIn("would update description", output)
        self.assertIn("would replace", output)
        self.assertIn("Unlock the Deep", output)
        self.assertIn("preflight passed", output)

    def test_a_submitted_version_cannot_take_a_new_build(self) -> None:
        for state in sorted(asc_release.SUBMITTED_VERSION_STATES):
            with self.subTest(state=state):
                code, output = self.preflight(FakeAsc(state=state, attached="B8"), SUBMIT_ONLY)
                self.assertEqual(code, 1)
                self.assertIn(f"already {state}", output)

    def test_submitted_version_with_text_switches_only_compares(self) -> None:
        code, output = self.preflight(FakeAsc(state="WAITING_FOR_REVIEW"), {"UPLOAD_LISTING": "true"})
        self.assertEqual(code, 0, output)
        code, _ = self.preflight(FakeAsc(state="WAITING_FOR_REVIEW", info_text={"subtitle": "Old"}),
                                 {"UPLOAD_LISTING": "true"})
        self.assertEqual(code, 1)

    def test_screenshots_apple_has_not_processed_are_never_taken_as_done(self) -> None:
        states = {IPAD: ["COMPLETE", "FAILED"]}
        code, output = self.preflight(FakeAsc(screenshots=self.repo_checksums(), screenshot_states=states),
                                      {"UPLOAD_SCREENSHOTS": "true"})
        self.assertEqual(code, 0, output)
        self.assertIn(f"{IPHONE} (en-US): 2 screenshot(s) already match the repo", output)
        self.assertIn(f"{IPAD} (en-US): screenshots match the repo but are not all processed", output)
        self.assertIn("would replace", output)
        # Once screenshots are locked, a failed image fails the preflight before the build.
        locked = FakeAsc(state="READY_FOR_REVIEW", screenshots=self.repo_checksums(), screenshot_states=states)
        code, output = self.preflight(locked, {"UPLOAD_SCREENSHOTS": "true"})
        self.assertEqual(code, 1, output)
        self.assertIn(f"{IPAD}-1: FAILED", output)

    def test_states_and_inputs_that_would_stop_the_store_step_fail_early(self) -> None:
        cases = {
            "live version": (FakeAsc(state="READY_FOR_SALE"), {"UPLOAD_LISTING": "true"}, "not editable"),
            "approved version, submit": (FakeAsc(state="PENDING_DEVELOPER_RELEASE"), SUBMIT_ONLY, "cannot be submitted"),
            "unlock without a version": (FakeAsc(unlock_state="READY_TO_SUBMIT", unlock_versions=[]), SUBMIT_ONLY,
                                         "cannot be added to App Review"),
            "locale missing in App Store Connect": (FakeAsc(locales=("de-DE",)), {"UPLOAD_WHATS_NEW": "true"},
                                                    "no en-US localization"),
            "App Info locked": (FakeAsc(state="READY_FOR_REVIEW", info_text={"subtitle": "Old"}),
                                {"UPLOAD_LISTING": "true"}, "no App Info is editable"),
        }
        for label, (fake, switches, message) in cases.items():
            if label == "App Info locked":
                fake.info_state = lambda: "WAITING_FOR_REVIEW"  # type: ignore[method-assign]
            with self.subTest(label):
                code, output = self.preflight(fake, switches)
                self.assertEqual(code, 1, output)
                self.assertIn(message, output)

    def test_preflight_needs_no_build_and_dry_run_is_separate(self) -> None:
        code, output = self.preflight(FakeAsc(), SUBMIT_ONLY)
        self.assertEqual(code, 0, output)
        code, output = self.run_main(FakeAsc(), {"UPLOAD_LISTING": "true"}, "--preflight", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("separate modes", output)

    def test_read_only_client_refuses_every_write(self) -> None:
        fake = FakeAsc()
        client = asc_release.ReadOnlyClient(fake)
        self.assertEqual(client.get("/apps?x")["data"][0]["id"], APP)
        for write in (lambda: client.post("/reviewSubmissions", {}),
                      lambda: client.patch("/appStoreVersions/V1", {}),
                      lambda: client.request("PATCH", "/reviewSubmissions/SUB1", body={}),
                      lambda: client.request("DELETE", "/appScreenshots/S1"),
                      lambda: client.upload_bytes("https://upload.example", b"", {})):
            with self.assertRaises(asc_release.ReadOnlyViolation):
                write()
        self.assertEqual(fake.writes(), [])


class CreateVersionTests(unittest.TestCase):
    """`asc_release.py --create-version` (IOS_CREATE_VERSION): add the tag's App Store version only when missing."""

    ON = {"CREATE_IOS_VERSION": "true"}

    def create(self, fake: FakeAsc, flags: dict[str, str] | None = None, *args: str,
               version: str | None = VERSION) -> tuple[int, str]:
        argv = ["asc_release.py", "--create-version", *(["--version", version] if version else []), *args]
        env = {k: v for k, v in os.environ.items() if k not in ASC_FLAGS}
        env.update(self.ON if flags is None else flags)
        out = io.StringIO()
        code = 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(asc_release, "client_from_environment", return_value=fake), \
                redirect_stdout(out), redirect_stderr(out):
            try:
                asc_release.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def test_switch_off_makes_no_calls(self) -> None:
        fake = FakeAsc(version_exists=False)
        code, output = self.create(fake, {})
        self.assertEqual(code, 0, output)
        self.assertIn("skipped", output)
        self.assertEqual(fake.calls, [])

    def test_existing_version_is_left_alone(self) -> None:
        fake = FakeAsc(state="READY_FOR_REVIEW")
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        self.assertIn(f"App Store version {VERSION} already exists (READY_FOR_REVIEW)", output)
        self.assertEqual(fake.writes(), [])

    def test_missing_version_is_created_for_abysm_and_read_back(self) -> None:
        fake = FakeAsc(version_exists=False)
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])
        body = fake.bodies[0][2]["data"]
        self.assertEqual(body["type"], "appStoreVersions")
        self.assertEqual(body["attributes"], {"platform": "IOS", "versionString": VERSION})
        self.assertEqual(body["relationships"]["app"]["data"], {"type": "apps", "id": APP})
        self.assertEqual(fake.calls[-1][0], "GET", "the created version must be read back")
        self.assertIn(f"created App Store version {VERSION} (PREPARE_FOR_SUBMISSION)", output)

    def test_a_created_version_that_does_not_read_back_once_fails_naming_it(self) -> None:
        # Not listed yet (0 records) or listed twice (2 records): the run stops and
        # points at App Store Connect, not at the switch that is already on.
        for listed in (0, 2):
            with self.subTest(listed=listed):
                fake = FakeAsc(version_exists=False)

                def post(path: str, body: dict, fake: FakeAsc = fake, listed: int = listed) -> dict:
                    fake.calls.append(("POST", path))
                    fake.bodies.append(("POST", path, body))
                    fake.version_exists = listed == 2
                    if listed == 2:
                        fake.other_versions = [*fake.other_versions, (VERSION, "PREPARE_FOR_SUBMISSION")]
                    return {"data": {"id": "V1"}}

                fake.post = post  # type: ignore[method-assign]
                code, output = self.create(fake)
                self.assertEqual(code, 1)
                self.assertIn(f"created App Store version {VERSION}, but reading it back found {listed} records; "
                              "check it in App Store Connect before rerunning", output)
                self.assertNotIn("turn on IOS_CREATE_VERSION", output)
                self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])

    def test_another_version_on_its_way_blocks_creation(self) -> None:
        for state in ("PREPARE_FOR_SUBMISSION", "WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE",
                      "REJECTED", ""):
            with self.subTest(state=state):
                fake = FakeAsc(version_exists=False,
                               other_versions=[("0.9", "REPLACED_WITH_NEW_VERSION"), ("1.0", state)])
                code, output = self.create(fake)
                self.assertEqual(code, 1)
                self.assertIn(f"version '1.0' is still {state or 'in an unknown state'}", output)
                self.assertIn("nothing was changed", output)
                self.assertNotIn("not newer", output)
                self.assertEqual(fake.writes(), [])

    def test_version_not_newer_than_an_existing_one_is_refused(self) -> None:
        for existing in ("1.1", "1.0.10", "2"):
            with self.subTest(existing=existing):
                fake = FakeAsc(version_exists=False, other_versions=[(existing, "READY_FOR_SALE")])
                code, output = self.create(fake)
                self.assertEqual(code, 1)
                self.assertIn(f"version {VERSION} is not newer than the existing version {existing}", output)
                self.assertEqual(fake.writes(), [])

    def test_older_finished_versions_allow_creation(self) -> None:
        finished = [("0.7", "DEVELOPER_REMOVED_FROM_SALE"), ("0.8", "REMOVED_FROM_SALE"),
                    ("0.9", "REPLACED_WITH_NEW_VERSION"), ("1.0", "READY_FOR_SALE")]
        self.assertEqual({state for _, state in finished}, asc_release.FINISHED_VERSION_STATES)
        fake = FakeAsc(version_exists=False, other_versions=finished)
        code, output = self.create(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [("POST", "/appStoreVersions")])

    def test_other_app_ambiguous_or_bad_version_fails_without_writing(self) -> None:
        fake = FakeAsc(app_id="1234567890", version_exists=False)
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn(f"expected {APP}", output)
        self.assertEqual(fake.writes(), [])
        fake = FakeAsc(other_versions=[(VERSION, "PREPARE_FOR_SUBMISSION")])
        code, output = self.create(fake)
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", output)
        self.assertEqual(fake.writes(), [])
        for bad in ("1.0.1-beta", "v1.0.1", "1.0.1.2", "1.x", "١.٠"):
            with self.subTest(bad=bad):
                fake = FakeAsc(version_exists=False)
                code, output = self.create(fake, version=bad)
                self.assertEqual(code, 1)
                self.assertIn("must look like 1.0 or 1.0.1", output)
                self.assertEqual(fake.calls, [])

    def test_dry_run_rejects_a_bad_version_like_a_live_run(self) -> None:
        for bad in ("1.0.1-beta", "v1.0.1", "1.0.1.2", "١.٠"):
            with self.subTest(bad=bad):
                fake = FakeAsc(version_exists=False)
                code, output = self.create(fake, None, "--dry-run", version=bad)
                self.assertEqual(code, 1)
                self.assertIn("must look like 1.0 or 1.0.1", output)
                self.assertNotIn("would create", output)
                self.assertEqual(fake.calls, [])

    def test_dry_run_preflight_and_missing_version_make_no_calls(self) -> None:
        fake = FakeAsc(version_exists=False)
        code, output = self.create(fake, None, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would create App Store version {VERSION}", output)
        code, output = self.create(fake, None, "--preflight")
        self.assertEqual(code, 1)
        self.assertIn("separate modes", output)
        code, output = self.create(fake, version=None)
        self.assertEqual(code, 1)
        self.assertIn("--create-version needs --version", output)
        self.assertEqual(fake.calls, [])


class CreativeGateTests(unittest.TestCase):
    """IOS_UPLOAD_CREATIVE_ASSETS follows every switch convention."""

    def test_creative_assets_are_their_own_switch(self) -> None:
        result = gates({"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_CREATIVE_ASSETS": "true"})
        self.assertTrue(result["creative"])
        self.assertTrue(result["asc"], "the store job runs for creative assets alone")
        self.assertFalse(result["build"])
        for key in OPS:
            self.assertFalse(result[key], key)
        self.assertFalse(gates({**ALL_ON, "IOS_UPLOAD_CREATIVE_ASSETS": "false"})["creative"])

    def test_it_needs_the_master_switch_and_validation_runs_never_use_it(self) -> None:
        result = gates({"IOS_UPLOAD_CREATIVE_ASSETS": "true"})
        self.assertFalse(result["creative"])
        self.assertFalse(result["asc"])
        self.assertTrue(any("IOS_UPLOAD_CREATIVE_ASSETS" in note for note in result["notes"]))
        manual = gates({**ALL_ON, "IOS_UPLOAD_CREATIVE_ASSETS": "true"}, event="workflow_dispatch", ref="main")
        self.assertFalse(manual["creative"])

    def test_invalid_values_fail_even_when_the_master_is_off(self) -> None:
        for env in ({"IOS_UPLOAD_CREATIVE_ASSETS": "maybe"},
                    {"IOS_RELEASE_AUTOMATION": "false", "IOS_UPLOAD_CREATIVE_ASSETS": "2"}):
            with self.subTest(env=env), self.assertRaisesRegex(ReleaseError, "IOS_UPLOAD_CREATIVE_ASSETS"):
                gates(env)

    def test_every_ios_switch_is_printed_and_documented(self) -> None:
        example = (ROOT / "store" / "gates.env.example").read_text()
        self.assertEqual(dict(ios_release.OPERATION_SWITCHES).get("IOS_UPLOAD_CREATIVE_ASSETS"), "creative")
        for name, _ in ios_release.OPERATION_SWITCHES:
            with self.subTest(name=name):
                self.assertIn(name, print_gates.GATES)
                self.assertIn(f"\n{name}=false\n", example)

    def test_print_gates_lists_it(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("IOS_")}
        for value, shown in (("true", "IOS_UPLOAD_CREATIVE_ASSETS=true\n"),
                             ("maybe", "IOS_UPLOAD_CREATIVE_ASSETS=INVALID 'maybe'")):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {**clean, "IOS_UPLOAD_CREATIVE_ASSETS": value}, clear=True), \
                    redirect_stdout(out):
                print_gates.main()
            self.assertIn(shown, out.getvalue())


class ScreenshotAlphaGuardTests(unittest.TestCase):
    """The last guard: a staged file that still stores alpha never reaches App Store Connect."""

    def test_alpha_is_detected_the_way_app_store_connect_refuses_it(self) -> None:
        cases = {
            "rgb": (make_png(2, 2, 2), False),
            "grey": (make_png(2, 2, 0), False),
            "palette": (make_png(2, 2, 3), False),
            "grey + alpha": (make_png(2, 2, 4), True),
            "rgba": (make_png(2, 2, 6), True),
            "rgb + tRNS": (make_png(2, 2, 2, chunks=((b"tRNS", b"\x00\x00\x00\x00\x00\x00"),)), True),
            "palette + tRNS": (make_png(2, 2, 3, chunks=((b"tRNS", b"\x00"),)), True),
        }
        for label, (raw, alpha) in cases.items():
            with self.subTest(label):
                self.assertEqual(asc_release.png_stores_alpha(asc_release.png_chunks(raw)), alpha)

    def test_truncated_or_corrupt_pngs_are_refused(self) -> None:
        good = make_png(8, 8)
        ihdr_end = 8 + 12 + 13
        idat = good.index(b"IDAT") - 4
        corrupt_crc = bytearray(good)
        corrupt_crc[idat + 8] ^= 0xFF  # first byte of the IDAT data; its CRC no longer matches
        # The IHDR chunk's CRC itself, its data intact: only the CRC check sees it.
        bad_crc_field = bytearray(good)
        bad_crc_field[ihdr_end - 1] ^= 0xFF
        cases = {
            "signature and IHDR only": good[:ihdr_end],
            "cut in the image data": good[: idat + 10],
            "no IEND": good[:-12],
            "IEND without its CRC": good[:-4],
            "bad CRC": bytes(corrupt_crc),
            "bad CRC field": bytes(bad_crc_field),
            "no IDAT": good[:ihdr_end] + good[-12:],
            "image data is not zlib": make_png(8, 8)[:idat] + self.chunk(b"IDAT", b"not zlib") + good[-12:],
            "image data for another size": make_png(8, 8)[:idat] + make_png(4, 4)[make_png(4, 4).index(b"IDAT") - 4:],
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                asc_release.png_chunks(raw)
        self.assertEqual([kind for kind, _ in asc_release.png_chunks(good)], [b"IHDR", b"IDAT", b"IEND"])
        # Lenient reading (a committed screenshot's size) only needs the header.
        self.assertEqual(asc_release.png_chunks(good[:ihdr_end], strict=False)[0][0], b"IHDR")

    @staticmethod
    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    def test_staged_files_with_alpha_or_not_png_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "01-home.png"
            for raw in (make_png(4, 4, 6), make_png(4, 4, 4), make_png(4, 4, 2, chunks=((b"tRNS", b"\x00" * 6),)),
                        b"\x89PNG\r\n\x1a\n" + b"screenshot" * 4, b"GIF89a", make_png(4, 4)[:-12]):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:30]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    asc_release.staged_png(path, IPHONE)
            path.write_bytes(make_png(4, 4, 2))
            self.assertEqual(asc_release.staged_png(path, IPHONE), (4, 4))

    def test_the_upload_step_refuses_a_file_that_still_stores_alpha_before_any_call(self) -> None:
        # The guard runs again right before upload, whatever produced the plan.
        with tempfile.TemporaryDirectory() as tmp:
            good, alpha = Path(tmp) / "01.png", Path(tmp) / "02.png"
            good.write_bytes(make_png(4, 4))
            alpha.write_bytes(make_png(4, 4, 6))
            client = ScreenshotUploadClient(existing=0)
            with self.assertRaises(SystemExit):
                quiet(asc_release.upload_screenshots, client, version_id="V1", locale="en-US",
                      plan=[(IPHONE, [good, alpha])], wait=lambda c, ids: None)
            self.assertEqual(client.calls, [])


class WatchScreenshotTests(MainFlow):
    """Apple Watch frames are uploaded like the other sets, by the display type their size belongs to."""

    def setUp(self) -> None:
        super().setUp()
        self.watch_png = self.frame(self.watch / "01-home-glance.png", 416, 496)

    def recorder(self, uploads: list) -> object:
        def upload(client: object, set_id: str, path: Path) -> str:
            uploads.append((set_id, path.name))
            return f"UP{len(uploads)}"

        return upload

    def test_each_watch_size_maps_to_its_display_type(self) -> None:
        self.frame(self.watch / "02-ultra-3.png", 422, 514)
        self.frame(self.watch / "03-ultra.png", 410, 502)
        self.frame(self.watch / "04-series-7.png", 396, 484)
        sets = asc_release.watch_screenshot_sets(self.watch)
        self.assertEqual(
            [(display, [p.name for p in pngs]) for display, pngs in sets],
            [("APP_WATCH_SERIES_10", ["01-home-glance.png"]),
             ("APP_WATCH_ULTRA", ["02-ultra-3.png", "03-ultra.png"]),
             ("APP_WATCH_SERIES_7", ["04-series-7.png"])],
        )
        expected = {(416, 496): "APP_WATCH_SERIES_10", (422, 514): "APP_WATCH_ULTRA", (410, 502): "APP_WATCH_ULTRA",
                    (396, 484): "APP_WATCH_SERIES_7", (368, 448): "APP_WATCH_SERIES_4", (312, 390): "APP_WATCH_SERIES_3"}
        self.assertEqual(asc_release.WATCH_DISPLAY_TYPES, expected)
        # The Watch sets come after the iPhone, iPad and iPhone Duo sets.
        self.frame(self.duo / "01-duo.png", 1398, 2034)
        plan = asc_release.screenshot_plan(self.sets, optional_sets=self.optional_sets, watch_dir=self.watch)
        self.assertEqual([display for display, _ in plan], [IPHONE, IPAD, "APP_IPHONE_DUO", "APP_WATCH_SERIES_10",
                                                             "APP_WATCH_ULTRA", "APP_WATCH_SERIES_7"])

    def test_a_frame_that_is_no_watch_size_or_stores_alpha_fails_before_any_call(self) -> None:
        # Staged without Pillow, a frame that stores alpha cannot be copied as it is.
        for name, size, colour_type in (("02-phone.png", (1290, 2796), 2), ("02-alpha.png", (416, 496), 6)):
            path = self.frame(self.watch / name, *size, colour_type)
            fake = FakeAsc()
            with self.subTest(name=name):
                code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
                self.assertEqual(code, 1, output)
                self.assertIn(name, output)
                self.assertEqual(fake.calls, [])
            path.unlink()
        # Eleven frames of one Watch size are one more than a set holds.
        for i in range(2, asc_release.APP_SCREENSHOT_SET_MAX + 2):
            self.frame(self.watch / f"{i:02}-more.png", 416, 496)
        fake = FakeAsc()
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 1, output)
        self.assertEqual(fake.calls, [])

    def test_the_watch_set_is_uploaded_with_its_display_type(self) -> None:
        fake = FakeAsc()
        uploads: list = []
        with mock.patch.object(asc_release, "upload_screenshot_file", self.recorder(uploads)):
            code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.created_sets, [IPHONE, IPAD, "APP_WATCH_SERIES_10"])
        self.assertEqual(uploads[-1], ("NEW-APP_WATCH_SERIES_10", "01-home-glance.png"))

    def test_a_watch_set_is_replaced_only_when_it_differs(self) -> None:
        live = self.repo_checksums()
        same = {**live, "APP_WATCH_SERIES_10": [asc_release.md5_hex(self.watch_png)]}
        fake = FakeAsc(screenshots=same)
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [])
        # A different live Watch set is replaced; the others are left alone.
        fake = FakeAsc(screenshots={**live, "APP_WATCH_SERIES_10": ["old"]})
        uploads: list = []
        with mock.patch.object(asc_release, "upload_screenshot_file", self.recorder(uploads)):
            code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(uploads, [("SET-APP_WATCH_SERIES_10", "01-home-glance.png")])
        self.assertEqual([c for c in fake.writes() if c[0] == "DELETE"], [("DELETE", "/appScreenshots/APP_WATCH_SERIES_10-0")])

    def test_a_locked_version_only_compares_the_watch_set(self) -> None:
        live = self.repo_checksums()
        same = {**live, "APP_WATCH_SERIES_10": [asc_release.md5_hex(self.watch_png)]}
        env = {"UPLOAD_SCREENSHOTS": "true", "SUBMIT_IOS_REVIEW": "true"}
        fake = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL", screenshots=same)
        code, output = self.run_main(fake, env)
        self.assertEqual(code, 0, output)
        self.assertFalse([c for c in fake.writes() if "creenshot" in c[1]])
        # A Watch set that differs while the version is in a review submission stops the run before any write.
        fake = FakeAsc(state="READY_FOR_REVIEW", attached="B9", release_type="MANUAL", screenshots=live)
        code, output = self.run_main(fake, env)
        self.assertEqual(code, 1, output)
        self.assertEqual(fake.writes(), [])

    def test_without_staged_watch_frames_the_live_watch_sets_are_left_alone(self) -> None:
        self.watch_png.unlink()
        fake = FakeAsc(screenshots={**self.repo_checksums(), "APP_WATCH_ULTRA": ["by hand"]})
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [])
        self.assertIn("no Apple Watch screenshots staged; the Watch sets in App Store Connect are left alone", output)
        self.assertEqual(asc_release.watch_screenshot_sets(self.watch), [])
        self.watch.rmdir()  # without the folder at all, nothing changes either
        self.assertEqual(asc_release.watch_screenshot_sets(self.watch), [])


class IphoneDuoScreenshotTests(MainFlow):
    """The iPhone Duo set is optional here: uploaded with the others only when its folder holds PNGs."""

    DUO = "APP_IPHONE_DUO"

    def test_without_frames_the_live_duo_set_is_left_alone(self) -> None:
        fake = FakeAsc(screenshots={**self.repo_checksums(), self.DUO: ["by hand"]})
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [])
        self.assertIn(f"no {self.DUO} screenshots staged; that set in App Store Connect is left alone", output)

    def test_duo_frames_go_with_the_other_sets(self) -> None:
        outer = self.frame(self.duo / "01-outer.png", 1398, 2034)
        inner = self.frame(self.duo / "02-inner.png", 2007, 2853)
        fake = FakeAsc(screenshots={**self.repo_checksums(), self.DUO: [asc_release.md5_hex(outer),
                                                                         asc_release.md5_hex(inner)]})
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [])
        self.assertIn(f"{self.DUO} (en-US): 2 screenshot(s) already match the repo", output)
        # A live set that differs is compared and replaced like the others.
        fake = FakeAsc(screenshots=self.repo_checksums())
        with mock.patch.object(asc_release, "upload_screenshot_file", return_value="UP"):
            code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.created_sets, [self.DUO])

    def test_a_duo_frame_of_another_size_fails_before_any_call(self) -> None:
        self.frame(self.duo / "01-phone.png", 1290, 2796)
        fake = FakeAsc()
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 1, output)
        self.assertIn("APP_IPHONE_DUO accepts 1398x2034, 2007x2853", output)
        self.assertEqual(fake.calls, [])


class ScreenshotStageTests(MainFlow):
    """The run uploads copies staged in a temporary folder; the committed files are only read (no Pillow)."""

    def stage(self) -> list:
        plan = asc_release.screenshot_plan(self.sets, optional_sets=self.optional_sets, watch_dir=self.watch)
        with mock.patch.object(asc_release, "try_import_pillow", return_value=None):
            return quiet(asc_release.stage_screenshots, plan, self.tmp / "staging")

    def test_watch_frames_are_staged_when_the_folder_exists(self) -> None:
        glance = self.frame(self.watch / "01-glance.png", 416, 496)
        ultra = self.frame(self.watch / "02-ultra.png", 422, 514)
        staged = dict(self.stage())
        self.assertEqual(list(staged), [IPHONE, IPAD, "APP_WATCH_SERIES_10", "APP_WATCH_ULTRA"])
        for display, source in (("APP_WATCH_SERIES_10", glance), ("APP_WATCH_ULTRA", ultra)):
            copy, = staged[display]
            self.assertEqual(copy.name, source.name)
            self.assertEqual(copy.parent, self.tmp / "staging" / display)
            self.assertEqual(copy.read_bytes(), source.read_bytes(), "without Pillow an RGB frame is copied as it is")
        # Without the folder, nothing is staged for the Watch.
        glance.unlink()
        ultra.unlink()
        self.watch.rmdir()
        self.assertEqual([display for display, _ in self.stage()], [IPHONE, IPAD])

    def test_upload_without_pillow_refuses_frames_that_store_alpha(self) -> None:
        cases = {
            "rgba": make_png(2796, 1290, 6),
            "grey + alpha": make_png(2796, 1290, 4),
            "rgb + tRNS": make_png(2796, 1290, 2, chunks=((b"tRNS", b"\x00" * 6),)),
        }
        iphone = self.sets[0][1]
        for label, raw in cases.items():
            (iphone / "02-alpha.png").write_bytes(raw)
            fake = FakeAsc()
            with self.subTest(label):
                code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, "--dry-run", build=False)
                self.assertEqual(code, 1, output)
                self.assertIn("02-alpha.png", output)
                self.assertIn("install Pillow", output)
                self.assertEqual((iphone / "02-alpha.png").read_bytes(), raw, "the committed file is untouched")
        # An RGB file is copied as it is.
        (iphone / "02-alpha.png").write_bytes(make_png(2796, 1290, 2))
        staged = dict(self.stage())
        self.assertEqual([p.read_bytes() for p in staged[IPHONE]], [p.read_bytes() for p in sorted(iphone.glob("*.png"))])


def chunk_types(raw: bytes) -> list[bytes]:
    return [kind for kind, _ in asc_release.png_chunks(raw)]


@needs_pillow
class RgbStageTests(MainFlow):
    """With Pillow, every App Store frame is staged as an RGB PNG without alpha."""

    OPAQUE = (10, 20, 30, 255)
    HIDDEN_RED = (255, 0, 0, 0)  # fully transparent, with a colour that must never show
    HALF = (200, 100, 50, 128)

    def setUp(self) -> None:
        if PILLOW is None:
            self.fail("Pillow is required here (STORE_TESTS_NEED_PILLOW is set)")
        super().setUp()
        self.pillow = PILLOW
        self.iphone, self.ipad = (folder for _, folder, _ in self.sets)

    def rgba(self, path: Path, size: tuple[int, int], *, icc: bytes | None = None,
             chunks: tuple[tuple[bytes, bytes], ...] = ()) -> Path:
        """A frame that stores alpha, like ABYSM's committed screenshots."""
        from PIL import PngImagePlugin

        img = PILLOW.new("RGBA", size, self.OPAQUE)
        width, height = size
        # The left quarter is transparent; one translucent pixel top right.
        img.paste(self.HIDDEN_RED, (0, 0, width // 4, height))
        img.putpixel((width - 1, 0), self.HALF)
        info = PngImagePlugin.PngInfo()
        for kind, data in chunks:
            info.add(kind, data)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, pnginfo=info, **({"icc_profile": icc} if icc else {}))
        return path

    def stage(self) -> dict[str, list[Path]]:
        plan = asc_release.screenshot_plan(self.sets, optional_sets=self.optional_sets, watch_dir=self.watch)
        with mock.patch.object(asc_release, "try_import_pillow", return_value=PILLOW):
            return dict(quiet(asc_release.stage_screenshots, plan, self.tmp / "staging"))

    def rgb(self, path: Path):
        raw = path.read_bytes()
        self.assertEqual(raw[25], 2, f"{path.name} is not an RGB PNG (colour type {raw[25]})")
        self.assertNotIn(b"tRNS", chunk_types(raw))
        img = PILLOW.open(io.BytesIO(raw))
        self.assertEqual(img.mode, "RGB")
        return img

    def test_frames_at_an_accepted_size_keep_their_pixels_over_black(self) -> None:
        source = self.rgba(self.iphone / "00.png", (2796, 1290))
        before = hashlib.sha256(source.read_bytes()).hexdigest()

        staged = self.stage()

        img = self.rgb(staged[IPHONE][0])
        self.assertEqual(img.size, (2796, 1290))
        self.assertEqual(img.getpixel((2795, 1289)), self.OPAQUE[:3], "opaque pixels are kept as they are")
        self.assertEqual(img.getpixel((0, 0)), (0, 0, 0), "transparent pixels are laid over black")
        # Translucent pixels are blended over black exactly once.
        for got, value in zip(img.getpixel((2795, 0)), self.HALF[:3]):
            self.assertAlmostEqual(got, value * 128 / 255, delta=1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before, "the committed file is untouched")

    def test_every_app_store_set_is_staged_without_alpha(self) -> None:
        self.rgba(self.iphone / "00.png", (2796, 1290))
        self.rgba(self.ipad / "00.png", (2752, 2064))
        self.rgba(self.duo / "01-d.png", (2007, 2853))
        self.rgba(self.watch / "01-w.png", (416, 496))
        self.rgba(self.watch / "02-w-ultra.png", (422, 514))

        staged = self.stage()

        self.assertEqual(list(staged), [IPHONE, IPAD, "APP_IPHONE_DUO", "APP_WATCH_SERIES_10", "APP_WATCH_ULTRA"])
        for display, copies in staged.items():
            for copy in copies:
                with self.subTest(set=display, name=copy.name):
                    self.rgb(copy)

    def test_the_colour_profile_is_kept(self) -> None:
        from PIL import ImageCms

        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        gamma = (b"gAMA", (45455).to_bytes(4, "big"))
        chroma = (b"cHRM", bytes(range(32)))
        self.rgba(self.iphone / "00.png", (2796, 1290), icc=icc, chunks=(gamma, chroma))

        staged = self.stage()[IPHONE][0]

        self.assertEqual(self.rgb(staged).info.get("icc_profile"), icc)
        kept = {kind: data for kind, data in asc_release.png_chunks(staged.read_bytes())}
        self.assertEqual(kept.get(b"gAMA"), gamma[1])
        self.assertEqual(kept.get(b"cHRM"), chroma[1])

    def test_the_run_uploads_the_rgb_copies_never_the_committed_files(self) -> None:
        for path in self.shots[IPHONE] + self.shots[IPAD]:
            with PILLOW.open(path) as img:
                size = img.size
            self.rgba(path, size)
        # Digests, not bytes: on a failure unittest would diff megabytes of PNG data
        # for longer than the Store release checks job may run.
        def digests() -> dict[Path, str]:
            return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.shots[IPHONE] + self.shots[IPAD]}

        committed = digests()
        sent: list[bytes] = []

        def upload(client: object, set_id: str, path: Path) -> str:
            sent.append(path.read_bytes())
            return f"UP{len(sent)}"

        fake = FakeAsc()
        with mock.patch.object(asc_release, "upload_screenshot_file", upload):
            code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(sent), 4)
        for raw in sent:
            self.assertEqual(raw[25], 2, "every upload is an RGB PNG")
            self.assertNotIn(hashlib.sha256(raw).hexdigest(), committed.values())
        self.assertEqual(digests(), committed, "the committed files are untouched")
        # The sets are compared with the copies' checksums: a re-run against them sends nothing.
        md5 = [hashlib.md5(raw).hexdigest() for raw in sent]  # noqa: S324 - Apple's checksum
        fake = FakeAsc(screenshots={IPHONE: md5[:2], IPAD: md5[2:]})
        code, output = self.run_main(fake, {"UPLOAD_SCREENSHOTS": "true"}, build=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(fake.writes(), [])


@functools.lru_cache(maxsize=None)
def creative_png(width: int, height: int, colour_type: int = 2) -> bytes:
    return make_png(width, height, colour_type)


def jpeg(width: int, height: int) -> bytes:
    """A JPEG header with a baseline start-of-frame segment (enough to read its size)."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x01\x11\x00" * 3
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def json_copy(value):
    return json.loads(json.dumps(value))


def related(item: dict, name: str):
    return ((item.get("relationships") or {}).get(name) or {}).get("data", {}).get("id")


def image_item(image_id: str) -> dict:
    return {"id": f"I-{image_id}", "relationships": {"appAssetLibraryImage": {"data": {"type": "appAssetLibraryImages", "id": image_id}}}}


def version_item(version_id: str = "V1") -> dict:
    return {"id": f"I-{version_id}", "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}


# The en-US localization of version V1 in FakeAsc.
LOC = "LOC-en-US"


class CreativeFake(FakeAsc):
    """FakeAsc plus the Asset Library, image placements and any number of review drafts."""

    def __init__(self, *, library: list[dict] | None = None, placements: dict[str, dict] | None = None,
                 drafts: list[str] | None = None, draft_items: dict[str, list[dict]] | None = None,
                 processing: dict[str, list[str]] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.library = [json_copy(image) for image in library or []]
        # placement id -> {"type", "image", "loc", "group", "state"}
        self.placements = {pid: dict(p) for pid, p in (placements or {}).items()}
        self.drafts = list(drafts or [])
        self.items = {draft: list(rows) for draft, rows in (draft_items or {}).items()}  # type: ignore[assignment]
        # image id -> the states successive GETs of the image report while Apple processes it
        self.processing = {image_id: list(states) for image_id, states in (processing or {}).items()}
        self.submitted: list[str] = []
        self.placed: list[dict] = []
        self.counter = 0

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def image(self, image_id: str) -> dict:
        return next(image for image in self.library if image["id"] == image_id)

    def get(self, path: str) -> dict:
        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)
        if path == f"/apps/{APP}/assetLibrary":
            self.calls.append(("GET", path))
            return {"data": {"type": "appAssetLibraries", "id": "LIB"}}
        if parsed.path == "/appAssetLibraries/LIB/images":
            self.calls.append(("GET", path))
            assert query.get("filter[category]") == ["CREATIVE_ASSETS"], path
            return {"data": [json_copy(image) for image in self.library]}
        if parsed.path.startswith("/appAssetLibraryImages/") and parsed.path.endswith("/relationships/placements"):
            self.calls.append(("GET", path))
            image_id = parsed.path.split("/")[2]
            return {"data": [{"type": "appAssetLibraryPlacements", "id": pid}
                             for pid, p in self.placements.items() if p["image"] == image_id]}
        if parsed.path.startswith("/appAssetLibraryImages/"):
            self.calls.append(("GET", path))
            image = self.image(parsed.path.split("/")[2])
            states = self.processing.get(image["id"])
            if states:
                image["attributes"]["state"] = states.pop(0)
                if image["attributes"]["state"] == "PREPARE_FOR_SUBMISSION":
                    image["attributes"]["imageAsset"] = {"width": 1, "height": 1}
            return {"data": json_copy(image)}
        if parsed.path == f"/appStoreVersionLocalizations/{LOC}/placements":
            self.calls.append(("GET", path))
            wanted = (query.get("filter[placementType]") or [""])[0]
            return {"data": [{"type": "appAssetLibraryPlacements", "id": pid,
                              "attributes": {"placementType": p["type"], "placementGroup": p.get("group", "DEFAULT_PROFILE"),
                                             "state": p.get("state", "ACTIVE")}}
                             for pid, p in self.placements.items() if p["loc"] == LOC and p["type"] == wanted]}
        if path.startswith(f"/apps/{APP}/reviewSubmissions"):
            self.calls.append(("GET", path))
            assert query.get("filter[state]") == ["READY_FOR_REVIEW"], path
            return {"data": [{"type": "reviewSubmissions", "id": draft} for draft in self.drafts]}
        if parsed.path.startswith("/reviewSubmissions/") and parsed.path.endswith("/items"):
            self.calls.append(("GET", path))
            assert "appAssetLibraryImage" in (query.get("include") or [""])[0], path
            return {"data": [json_copy(item) for item in self.items.get(parsed.path.split("/")[2], [])]}
        return super().get(path)

    def post(self, path: str, body: dict) -> dict:
        data = body["data"]
        if path == "/appAssetLibraryImages":
            self.calls.append(("POST", path))
            image_id = self.next_id("IMG")
            attributes = data["attributes"]
            self.library.append({"id": image_id, "attributes": {**attributes, "state": "AWAITING_UPLOAD",
                                                                 "createdDate": "2026-10-21"}})
            ops = [{"url": f"https://upload.example/{image_id}", "offset": 0, "length": attributes["fileSize"],
                    "method": "PUT", "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}]
            return {"data": {"type": "appAssetLibraryImages", "id": image_id,
                             "attributes": {"state": "AWAITING_UPLOAD", "uploadOperations": ops}}}
        if path == "/reviewSubmissions":
            self.calls.append(("POST", path))
            draft = self.next_id("DRAFT")
            self.drafts.append(draft)
            self.items[draft] = []
            return {"data": {"type": "reviewSubmissions", "id": draft}}
        if path == "/reviewSubmissionItems":
            self.calls.append(("POST", path))
            rels = data["relationships"]
            draft = rels["reviewSubmission"]["data"]["id"]
            (name, related_data), = [(k, v) for k, v in rels.items() if k != "reviewSubmission"]
            self.items.setdefault(draft, []).append(
                {"id": self.next_id("ITEM"), "relationships": {name: {"data": dict(related_data["data"])}}})
            if name == "appAssetLibraryImage":
                self.image(related_data["data"]["id"])["attributes"]["state"] = "READY_FOR_REVIEW"
            return {"data": {"type": "reviewSubmissionItems", "id": "ITEM"}}
        if path == "/appAssetLibraryPlacements":
            self.calls.append(("POST", path))
            self.placed.append(body)
            rels = data["relationships"]
            swap = (rels.get("placementToSwapOut") or {}).get("data")
            if swap:
                del self.placements[swap["id"]]
            self.placements[self.next_id("PL")] = {
                "type": data["attributes"]["placementType"], "image": rels["image"]["data"]["id"],
                "loc": rels["appStoreVersionLocalization"]["data"]["id"],
                "group": data["attributes"]["placementGroup"]}
            return {"data": {"type": "appAssetLibraryPlacements", "id": "PL"}}
        return super().post(path, body)

    def patch(self, path: str, body: dict) -> dict:
        if path.startswith("/appAssetLibraryImages/"):
            self.calls.append(("PATCH", path))
            image = self.image(path.split("/")[2])
            assert body["data"]["attributes"] == {"uploaded": True}
            image["attributes"]["state"] = "UPLOAD_COMPLETE"
            self.processing.setdefault(image["id"], ["UPLOAD_COMPLETE", "PREPARE_FOR_SUBMISSION"])
            return {"data": body["data"]}
        return super().patch(path, body)

    def request(self, method: str, path: str, body: dict | None = None):
        if method == "PATCH" and path.startswith("/reviewSubmissions/"):
            self.calls.append((method, path))
            draft = path.split("/")[2]
            assert body is not None and body["data"]["attributes"] == {"submitted": True}
            self.submitted.append(draft)
            self.drafts.remove(draft)
            for item in self.items.get(draft, []):
                image_id = related(item, "appAssetLibraryImage")
                if image_id:
                    self.image(image_id)["attributes"]["state"] = "WAITING_FOR_REVIEW"
            return None
        return super().request(method, path, body)

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.calls.append(("UPLOAD", url))
        image = self.image(url.rsplit("/", 1)[1])
        assert len(data) == image["attributes"]["fileSize"] and headers == {"Content-Type": "image/png"}


class CreativeAssetTests(MainFlow):
    """Header and search results images: Asset Library upload, their own review, placement."""

    ENV = {"UPLOAD_CREATIVE_ASSETS": "true"}

    def setUp(self) -> None:
        super().setUp()
        self.dir = self.creative_dir
        self.header = self.dir / "header" / "header.png"
        self.search = self.dir / "search-results" / "search.png"
        for path, raw in ((self.header, creative_png(3840, 1646)), (self.search, creative_png(1920, 1280))):
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
        sleep = mock.patch.object(asc_release.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def image(self, image_id: str, path: Path, state: str, created: str = "2026-10-20") -> dict:
        return {"id": image_id, "attributes": {"fileName": path.name, "fileSize": path.stat().st_size,
                                               "state": state, "createdDate": created,
                                               "imageAsset": {"width": 1, "height": 1}}}

    def run_creative(self, fake: FakeAsc, env: dict[str, str] | None = None, *args: str,
                     creative_dir: Path | None = None, build: bool = False) -> int:
        """asc_release.main() with creative assets on; returns the exit code (output in self.output)."""
        if creative_dir is not None:
            args = (*args, "--creative-dir", str(creative_dir))
        code, self.output = self.run_main(fake, self.ENV if env is None else env, *args, build=build)
        return code

    def submit(self, fake: FakeAsc) -> int:
        return self.run_creative(fake, {**self.ENV, "SUBMIT_IOS_REVIEW": "true"}, build=True)

    def preflight(self, fake: FakeAsc, *, submit: bool = False) -> int:
        """The read-only preflight: any write raises before it is sent."""
        env = {**self.ENV, **({"SUBMIT_IOS_REVIEW": "true"} if submit else {})}
        return self.run_creative(fake, env, "--preflight")

    def image_review_drafts(self, fake: CreativeFake) -> set[str]:
        return {draft for draft, items in fake.items.items() if any(related(i, "appAssetLibraryImage") for i in items)}

    def test_new_images_are_uploaded_once_and_reviewed_on_their_own(self) -> None:
        fake = CreativeFake()
        self.assertEqual(self.run_creative(fake), 0, self.output)
        posts = [c for c in fake.writes() if c == ("POST", "/appAssetLibraryImages")]
        self.assertEqual(len(posts), 2)
        self.assertEqual([c[0] for c in fake.writes() if c[1].startswith(("https://upload", "/appAssetLibraryImages/"))],
                         ["UPLOAD", "PATCH", "UPLOAD", "PATCH"])
        uploaded = {image["attributes"]["fileName"]: image["attributes"] for image in fake.library}
        self.assertEqual(set(uploaded), {"header.png", "search.png"})
        self.assertEqual({a["category"] for a in uploaded.values()}, {"CREATIVE_ASSETS"})
        self.assertEqual(uploaded["header.png"]["fileSize"], self.header.stat().st_size)
        # One submission of their own, holding exactly the two images, submitted.
        self.assertEqual(len(fake.submitted), 1)
        held = [related(item, "appAssetLibraryImage") for item in fake.items[fake.submitted[0]]]
        self.assertEqual(sorted(held), sorted(image["id"] for image in fake.library))
        self.assertEqual(fake.placed, [], "images are placed only once Apple approves them")
        # The next run finds them in review: nothing is uploaded, reviewed or placed again.
        before = len(fake.writes())
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(len(fake.writes()), before)

    def test_empty_creative_folders_mean_nothing_to_do(self) -> None:
        # ABYSM's case: the folders exist without an image, so no Asset Library call is made.
        self.header.unlink()
        self.search.unlink()
        (self.header.parent / "README.md").write_text("notes")
        fake = CreativeFake()
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(fake.writes(), [])
        self.assertFalse([c for c in fake.calls if "assetLibrar" in c[1] or "appAssetLibrary" in c[1]], fake.calls)
        self.assertIn("has no header or search results image; nothing to do", self.output)

    def test_approved_images_are_placed_on_the_version(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "APPROVED")],
                            # Placed on another version's localization only.
                            placements={"PL-OLD": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "LOC-712"}})
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(fake.placed, [
            {"data": {"type": "appAssetLibraryPlacements",
                      "attributes": {"placementType": placement, "placementGroup": "DEFAULT_PROFILE"},
                      "relationships": {"image": {"data": {"type": "appAssetLibraryImages", "id": image_id}},
                                        "appStoreVersionLocalization": {"data": {"type": "appStoreVersionLocalizations",
                                                                                 "id": LOC}}}}}
            for placement, image_id in (("PRODUCT_PAGE_HEADER_ASSET", "H"), ("APP_STORE_SEARCH_RESULTS_ASSET", "S"))
        ])
        self.assertNotIn(("POST", "/appAssetLibraryImages"), fake.calls)
        self.assertEqual(fake.submitted, [])
        # A second run finds both placed and writes nothing.
        before = len(fake.writes())
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(len(fake.writes()), before)

    def test_another_image_in_the_slot_is_swapped_out_in_one_request(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")],
                            placements={"PL-OLD": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "OLDIMG", "loc": LOC}})
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(len(fake.placed), 1)
        rels = fake.placed[0]["data"]["relationships"]
        self.assertEqual(rels["placementToSwapOut"], {"data": {"type": "appAssetLibraryPlacements", "id": "PL-OLD"}})
        self.assertEqual(rels["image"]["data"]["id"], "H")
        self.assertNotIn("PL-OLD", fake.placements)

    def test_an_interrupted_upload_is_uploaded_again(self) -> None:
        stale = self.image("STALE", self.header, "AWAITING_UPLOAD", created="2026-10-21")
        fake = CreativeFake(library=[stale, self.image("S", self.search, "APPROVED")])
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual([c for c in fake.writes() if c == ("POST", "/appAssetLibraryImages")],
                         [("POST", "/appAssetLibraryImages")])
        reviewed = {related(item, "appAssetLibraryImage") for item in fake.items[fake.submitted[0]]}
        self.assertNotIn("STALE", reviewed)
        self.assertEqual(len(reviewed), 1)
        # Failed and archived images do not count either.
        for state in ("FAILED", "ARCHIVED"):
            fake = CreativeFake(library=[self.image("OLD", self.header, state), self.image("S", self.search, "APPROVED")])
            with self.subTest(state=state):
                self.assertEqual(self.run_creative(fake), 0, self.output)
                self.assertIn(("POST", "/appAssetLibraryImages"), fake.calls)

    def test_an_uploaded_image_still_processing_is_reviewed_once_ready(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "UPLOAD_COMPLETE"), self.image("S", self.search, "APPROVED")],
                            processing={"H": ["UPLOAD_COMPLETE", "COMPLETE", "PREPARE_FOR_SUBMISSION"]})
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertNotIn(("POST", "/appAssetLibraryImages"), fake.calls)
        self.assertEqual(fake.processing["H"], [], "the run waits until Apple has processed the image")
        self.assertEqual([related(i, "appAssetLibraryImage") for i in fake.items[fake.submitted[0]]], ["H"])

    def test_processing_failure_or_timeout_stops_before_review(self) -> None:
        fake = CreativeFake(library=[self.image("H", self.header, "UPLOAD_COMPLETE")], processing={"H": ["FAILED"]})
        self.assertEqual(self.run_creative(fake), 1, self.output)
        self.assertEqual(fake.submitted, [])
        self.assertNotIn(("POST", "/reviewSubmissionItems"), fake.calls)
        fake = CreativeFake(library=[self.image("H", self.header, "UPLOAD_COMPLETE")],
                            processing={"H": ["UPLOAD_COMPLETE"] * 5})
        with mock.patch.object(asc_release, "CREATIVE_PROCESSING_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.run_creative(fake), 1, self.output)
        self.assertNotIn(("POST", "/reviewSubmissionItems"), fake.calls)

    def test_images_in_review_are_neither_uploaded_nor_placed(self) -> None:
        for state in ("WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED"):
            fake = CreativeFake(library=[self.image("H", self.header, state), self.image("S", self.search, state)])
            with self.subTest(state=state):
                self.assertEqual(self.run_creative(fake), 0, self.output)
                self.assertEqual(fake.writes(), [])

    def test_a_rejected_image_stops_the_run_before_any_write(self) -> None:
        rejected = self.image("H", self.header, "REJECTED")
        rejected["attributes"]["stateDetails"] = [{"code": "IMAGE_QUALITY"}]
        fake = CreativeFake(library=[rejected, self.image("S", self.search, "PREPARE_FOR_SUBMISSION")])
        self.assertEqual(self.run_creative(fake), 1, self.output)
        self.assertEqual(fake.writes(), [])
        self.assertIn("IMAGE_QUALITY", self.output)

    def test_a_locked_version_takes_no_new_images(self) -> None:
        for state, attached in (("WAITING_FOR_REVIEW", "B9"), ("READY_FOR_REVIEW", "B9")):
            for library in ([], [self.image("H", self.header, "PREPARE_FOR_SUBMISSION")]):
                fake = CreativeFake(state=state, attached=attached, library=library)
                with self.subTest(state=state, library=bool(library)):
                    self.assertEqual(self.run_creative(fake), 1, self.output)
                    self.assertEqual(fake.writes(), [])

    def test_an_approved_image_on_a_locked_version_waits_without_failing(self) -> None:
        # Approval can land between a submit and a re-run of the store job.
        fake = CreativeFake(state="WAITING_FOR_REVIEW", attached="B9",
                            library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")])
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(fake.writes(), [])

    def test_images_never_go_into_the_versions_draft(self) -> None:
        # The version's draft is picked (and reserved) before any write; the
        # empty draft goes to the version and the images get their own submission.
        fake = CreativeFake(drafts=["EMPTY"], draft_items={"EMPTY": []})
        self.assertEqual(self.submit(fake), 0, self.output)
        self.assertEqual(len(fake.submitted), 2)
        self.assertEqual(fake.submitted[-1], "EMPTY", "the version went out in the reserved draft")
        self.assertEqual([related(i, "appStoreVersion") for i in fake.items["EMPTY"]], ["V1"])
        self.assertEqual(self.image_review_drafts(fake), {fake.submitted[0]})
        self.assertNotIn("EMPTY", self.image_review_drafts(fake))

    def test_a_creative_draft_is_never_used_for_the_version(self) -> None:
        library = [self.image("H", self.header, "WAITING_FOR_REVIEW"), self.image("S", self.search, "APPROVED")]
        placements = {"P": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "S", "loc": LOC}}
        # A draft holding an image (left by hand or another tool) is not the version's.
        fake = CreativeFake(library=library, placements=placements, drafts=["IMAGES"],
                            draft_items={"IMAGES": [image_item("OTHER")]})
        self.assertEqual(self.submit(fake), 0, self.output)
        self.assertEqual(fake.items["IMAGES"], [image_item("OTHER")])
        self.assertEqual(len(fake.submitted), 1)
        self.assertNotEqual(fake.submitted[0], "IMAGES")
        self.assertIn(("POST", "/reviewSubmissions"), fake.calls)

    def test_a_ready_for_review_version_its_draft_does_not_hold_stops_before_any_write(self) -> None:
        # Images the locked version can take (one in review, one approved and placed), so only the draft stops it.
        library = [self.image("H", self.header, "IN_REVIEW"), self.image("S", self.search, "APPROVED")]
        placements = {"P": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "S", "loc": LOC}}
        # The only draft holding the version also holds an image, so it is not the version's draft.
        for drafts, items in (([], {}), (["MIXED"], {"MIXED": [version_item(), image_item("X")]})):
            fake = CreativeFake(state="READY_FOR_REVIEW", library=library, placements=placements,
                                drafts=drafts, draft_items=items)
            with self.subTest(drafts=drafts):
                self.assertEqual(self.submit(fake), 1, self.output)
                self.assertIn("no draft review submission without header or search results images holds it",
                              self.output)
                self.assertEqual(fake.writes(), [])

    def test_the_draft_holding_this_version_is_reserved_over_others(self) -> None:
        fake = CreativeFake(state="READY_FOR_REVIEW", drafts=["EMPTY", "MINE"],
                            draft_items={"EMPTY": [], "MINE": [version_item()]},
                            library=[self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")],
                            placements={"P": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": LOC}})
        self.assertEqual(self.submit(fake), 0, self.output)
        self.assertEqual(fake.submitted, ["MINE"])
        self.assertEqual(fake.items["EMPTY"], [])

    def test_a_rerun_finishes_the_draft_an_interrupted_review_left(self) -> None:
        library = [self.image("H", self.header, "READY_FOR_REVIEW"), self.image("S", self.search, "PREPARE_FOR_SUBMISSION")]
        # A draft already holding some of the images is finished before an empty one.
        fake = CreativeFake(library=library, drafts=["EMPTY", "PART"],
                            draft_items={"EMPTY": [], "PART": [image_item("H")]})
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertNotIn(("POST", "/reviewSubmissions"), fake.calls)
        self.assertEqual(fake.submitted, ["PART"])
        self.assertEqual(sorted(related(i, "appAssetLibraryImage") for i in fake.items["PART"]), ["H", "S"])
        self.assertEqual(fake.items["EMPTY"], [])
        # Else an empty draft (a run stopped before it added the images) is used.
        fake = CreativeFake(library=[self.image("S", self.search, "PREPARE_FOR_SUBMISSION")],
                            drafts=["EMPTY"], draft_items={"EMPTY": []})
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertNotIn(("POST", "/reviewSubmissions"), fake.calls)
        self.assertEqual(fake.submitted, ["EMPTY"])
        # A draft holding anything else is left alone and a new one is created.
        fake = CreativeFake(library=[self.image("S", self.search, "PREPARE_FOR_SUBMISSION")],
                            drafts=["OTHER"], draft_items={"OTHER": [version_item("V0")]})
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertIn(("POST", "/reviewSubmissions"), fake.calls)
        self.assertEqual(fake.items["OTHER"], [version_item("V0")])
        self.assertNotIn("OTHER", fake.submitted)

    def test_a_creative_submission_holding_other_items_is_not_submitted(self) -> None:
        class Crowded(CreativeFake):
            def post(self, path: str, body: dict) -> dict:
                result = super().post(path, body)
                if path == "/reviewSubmissions":
                    # Something else lands in the new draft before it is submitted.
                    self.items[result["data"]["id"]].append(version_item("V0"))
                return result

        fake = Crowded(library=[self.image("S", self.search, "PREPARE_FOR_SUBMISSION")])
        self.assertEqual(self.run_creative(fake), 1, self.output)
        self.assertEqual(fake.submitted, [])

    def test_images_apple_would_refuse_fail_before_any_call(self) -> None:
        cases = [
            (self.header, creative_png(3840, 2160), "header.png"),
            (self.header, creative_png(3840, 1646, 6), "header.png"),
            (self.header, jpeg(3840, 1646), "header.jpg"),
            (self.header, jpeg(3840, 1646), "header.png"),
            (self.search, creative_png(3000, 2001), "search.png"),
            (self.search, creative_png(1800, 1200), "search.png"),
            (self.search, creative_png(3900, 2600), "search.png"),
            (self.search, creative_png(1920, 1280, 4), "search.png"),
            (self.search, make_png(1920, 1280, 2, chunks=((b"tRNS", b"\x00" * 6),)), "search.png"),
            (self.search, jpeg(4000, 2666), "search.jpg"),
            (self.search, b"GIF89a", "search.png"),
            # Truncated or corrupt files with the right header fail too.
            (self.header, creative_png(3840, 1646)[:-12], "header.png"),
            (self.header, creative_png(3840, 1646)[:8 + 25], "header.png"),
            (self.search, creative_png(1920, 1280)[: len(creative_png(1920, 1280)) // 2], "search.png"),
        ]
        for path, raw, name in cases:
            original = path.read_bytes()
            path.unlink()
            (path.parent / name).write_bytes(raw)
            fake = CreativeFake()
            with self.subTest(slot=path.parent.name, name=name, raw=raw[:26]):
                self.assertEqual(self.run_creative(fake), 1, self.output)
                self.assertEqual(fake.calls, [])
            (path.parent / name).unlink()
            path.write_bytes(original)

    def test_sizes_and_formats_apple_takes_pass(self) -> None:
        for name, raw in (("search.jpg", jpeg(1920, 1280)), ("search.jpeg", jpeg(3840, 2560)),
                          ("search.png", creative_png(3840, 2560)), ("search.png", creative_png(3000, 2000))):
            self.search.unlink()
            (self.search.parent / name).write_bytes(raw)
            with self.subTest(name=name, raw=raw[:26]):
                sources = quiet(asc_release.creative_sources, self.dir)
                self.assertEqual([(p, path.name) for p, _, path in sources],
                                 [("PRODUCT_PAGE_HEADER_ASSET", "header.png"), ("APP_STORE_SEARCH_RESULTS_ASSET", name)])
            (self.search.parent / name).unlink()
            self.search.write_bytes(creative_png(1920, 1280))

    def test_a_slot_takes_one_image_and_empty_folders_mean_nothing_to_do(self) -> None:
        (self.header.parent / "header-2.png").write_bytes(creative_png(3840, 1646))
        fake = CreativeFake()
        self.assertEqual(self.run_creative(fake), 1, self.output)
        self.assertEqual(fake.calls, [])
        (self.header.parent / "header-2.png").unlink()
        # Other files (notes, .DS_Store) do not count; an empty slot is left alone.
        (self.header.parent / "README.md").write_text("notes")
        self.header.unlink()
        self.assertEqual([p for p, _, _ in quiet(asc_release.creative_sources, self.dir)],
                         ["APP_STORE_SEARCH_RESULTS_ASSET"])
        # Without any image there is nothing to do (Fud AI fails here; ABYSM has no image yet).
        self.search.unlink()
        self.assertEqual(quiet(asc_release.creative_sources, self.dir), [])
        fake = CreativeFake()
        self.assertEqual(self.run_creative(fake), 0, self.output)
        self.assertEqual(fake.writes(), [])

    @repo_content
    def test_the_repository_images_pass_the_local_checks(self) -> None:
        # ABYSM has no header or search results image yet: both folders hold only a README
        # (hidden files such as Finder's .DS_Store, which git ignores, do not count).
        for folder, _, _, _ in asc_release.CREATIVE_SLOTS:
            with self.subTest(folder=folder):
                slot = asc_release.DEFAULT_CREATIVE_DIR / folder
                self.assertEqual(sorted(p.name for p in slot.iterdir() if not p.name.startswith(".")), ["README.md"])
        self.assertEqual(quiet(asc_release.creative_sources, asc_release.DEFAULT_CREATIVE_DIR), [])

    def test_preflight_reads_the_switch(self) -> None:
        # Approved and placed images: the preflight passes without writing.
        library = [self.image("HDR", self.header, "APPROVED"), self.image("SRCH", self.search, "APPROVED")]
        placements = {"PL-HDR": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "HDR", "loc": LOC},
                      "PL-SRCH": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "SRCH", "loc": LOC}}
        fake = CreativeFake(library=library, placements=placements)
        self.assertEqual(self.preflight(fake), 0, self.output)
        # The images go on the version's localization, so the preflight checks it, and their library state.
        self.assertTrue([c for c in fake.calls if "appStoreVersionLocalizations" in c[1]])
        self.assertIn(("GET", f"/apps/{APP}/assetLibrary"), fake.calls)
        self.assertEqual(fake.writes(), [])
        # With the switch off, the preflight never reads the Asset Library.
        fake = CreativeFake(library=library, placements=placements)
        self.assertEqual(self.run_creative(fake, {"UPLOAD_LISTING": "true"}, "--preflight"), 0, self.output)
        self.assertNotIn(("GET", f"/apps/{APP}/assetLibrary"), fake.calls)

    def test_the_preflight_stops_before_the_build_when_the_version_cannot_take_the_images(self) -> None:
        # In a review submission the version takes no new image: found before the build, not after.
        for library in ([], [self.image("H", self.header, "PREPARE_FOR_SUBMISSION")]):
            fake = CreativeFake(state="READY_FOR_REVIEW", library=library,
                                drafts=["MINE"], draft_items={"MINE": [version_item()]})
            with self.subTest(library=bool(library)):
                self.assertEqual(self.preflight(fake), 1, self.output)
            self.assertEqual(fake.writes(), [])
        # A rejected image stops it in any state.
        rejected = [self.image("H", self.header, "REJECTED"), self.image("S", self.search, "APPROVED")]
        self.assertEqual(self.preflight(CreativeFake(library=rejected)), 1, self.output)

    def test_the_preflight_passes_what_the_store_step_can_do(self) -> None:
        cases = [
            # An editable version takes new images (uploaded by the store step, not here).
            ("PREPARE_FOR_SUBMISSION", []),
            # Images in review or approved wait for a later run without failing.
            ("READY_FOR_REVIEW", [self.image("H", self.header, "IN_REVIEW"), self.image("S", self.search, "APPROVED")]),
        ]
        for state, library in cases:
            fake = CreativeFake(state=state, library=library, drafts=["MINE"], draft_items={"MINE": [version_item()]})
            with self.subTest(state=state):
                self.assertEqual(self.preflight(fake, submit=True), 0, self.output)
                self.assertEqual(fake.writes(), [])
                self.assertIn("preflight passed", self.output)
        self.assertNotIn("would upload", self.output)
        self.assertEqual(self.preflight(CreativeFake(), submit=True), 0, self.output)
        self.assertIn("product page header: would upload header.png to the Asset Library", self.output)

    def test_the_preflight_checks_a_ready_for_review_versions_draft(self) -> None:
        library = [self.image("H", self.header, "APPROVED"), self.image("S", self.search, "IN_REVIEW")]
        placements = {"P": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": LOC}}
        for drafts, items in (([], {}), (["MIXED"], {"MIXED": [version_item(), image_item("X")]})):
            fake = CreativeFake(state="READY_FOR_REVIEW", library=library, placements=placements,
                                drafts=drafts, draft_items=items)
            with self.subTest(drafts=drafts):
                self.assertEqual(self.preflight(fake, submit=True), 1, self.output)
            self.assertEqual(fake.writes(), [])
            # Without review submission the draft does not matter.
            self.assertEqual(self.preflight(fake, submit=False), 0, self.output)

    def test_dry_run_checks_the_images_without_calls(self) -> None:
        # (The repository's own folders are checked by test_the_repository_images_pass_the_local_checks.)
        fake = CreativeFake()
        self.assertEqual(self.run_creative(fake, None, "--dry-run"), 0, self.output)
        self.assertEqual(fake.calls, [])
        self.assertIn("creative=True", self.output)
        self.header.write_bytes(creative_png(3840, 1646, 6))
        fake = CreativeFake()
        self.assertEqual(self.run_creative(fake, None, "--dry-run"), 1, self.output)
        self.assertEqual(fake.calls, [])


class ReleaseNotesTests(unittest.TestCase):
    TEXT = "# Notes\n\n## Unreleased\n\n- next\n\n## v1.0.1\n\n- Fixes\n\n## v1.0.2\n\n"

    def test_exact_tag_section(self) -> None:
        self.assertEqual(release_notes.section(self.TEXT, "v1.0.1"), "- Fixes")
        self.assertIsNone(release_notes.section(self.TEXT, "v1.0"))
        self.assertIsNone(release_notes.section(self.TEXT, "Unreleased"))
        self.assertEqual(release_notes.section(self.TEXT, "v1.0.2"), "")

    def test_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "RELEASE_NOTES.md"
            notes.write_text(self.TEXT)
            with mock.patch.object(release_notes, "NOTES", notes):
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    self.assertEqual(release_notes.main(["v1.0.1"]), 0)
                    self.assertEqual(release_notes.main(["v9.9"]), release_notes.MISSING)
                    self.assertEqual(release_notes.main(["v1.0.2"]), 1)
                self.assertEqual(out.getvalue().strip(), "- Fixes")

    def test_missing_notes_file_means_generated_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(release_notes, "NOTES", Path(tmp) / "RELEASE_NOTES.md"):
                self.assertEqual(quiet(release_notes.main, ["v1.0.1"]), release_notes.MISSING)

    def test_duplicate_sections_for_the_tag_are_rejected(self) -> None:
        text = self.TEXT + "## v1.0.1\n\n- A second, different section\n"
        with self.assertRaisesRegex(ValueError, "2 '## v1.0.1' sections"):
            release_notes.section(text, "v1.0.1")
        self.assertEqual(release_notes.section(text, "v1.0.2"), "")  # other tags are unaffected
        self.assertEqual(release_notes.duplicate_headings(text), ["v1.0.1"])
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "RELEASE_NOTES.md"
            notes.write_text(text)
            err = io.StringIO()
            with mock.patch.object(release_notes, "NOTES", notes), redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(release_notes.main(["v1.0.1"]), 1)
            self.assertIn("keep exactly one", err.getvalue())

    @repo_content
    def test_repository_release_notes_have_one_section_per_heading(self) -> None:
        text = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertEqual(release_notes.duplicate_headings(text), [])


class PrintGatesTests(unittest.TestCase):
    def output(self, env: dict[str, str]) -> str:
        names = print_gates.GATES + tuple(name for name, *_ in print_gates.TIMING)
        clean = {k: v for k, v in os.environ.items() if k not in names}
        out = io.StringIO()
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), redirect_stdout(out):
            print_gates.main()
        return out.getvalue()

    def test_invalid_values_are_shown_as_invalid_not_off(self) -> None:
        output = self.output({"IOS_RELEASE_AUTOMATION": "true", "IOS_UPLOAD_BUILD": "maybe",
                              "IOS_BUILD_POLL_SECONDS": "5", "IOS_BUILD_WAIT_MINUTES": "ninety"})
        self.assertIn("IOS_UPLOAD_BUILD=INVALID 'maybe' (a release run fails)", output)
        self.assertIn("IOS_BUILD_POLL_SECONDS=INVALID '5'", output)
        self.assertIn("IOS_BUILD_WAIT_MINUTES=INVALID 'ninety'", output)
        self.assertIn("ERROR: IOS_UPLOAD_BUILD, IOS_BUILD_POLL_SECONDS, IOS_BUILD_WAIT_MINUTES invalid", output)

    def test_valid_unset_and_default_values(self) -> None:
        output = self.output({"IOS_RELEASE_AUTOMATION": " Yes ", "IOS_UPLOAD_LISTING": "off",
                              "IOS_BUILD_WAIT_MINUTES": "300"})
        self.assertIn("IOS_RELEASE_AUTOMATION=true", output)
        self.assertIn("IOS_UPLOAD_LISTING=false\n", output)
        self.assertIn("IOS_SUBMIT_REVIEW=false (unset)", output)
        self.assertIn("IOS_BUILD_POLL_SECONDS=30 (default)", output)
        self.assertIn("IOS_BUILD_WAIT_MINUTES=300", output)
        self.assertNotIn("INVALID", output)
        self.assertIn("no store operation is enabled", output)
        self.assertIn("IOS_RELEASE_AUTOMATION is off", self.output({}))

    def test_same_rules_as_the_gates(self) -> None:
        self.assertEqual(print_gates.GATES, (ios_release.MASTER,) + tuple(n for n, _ in ios_release.OPERATION_SWITCHES))
        self.assertEqual(print_gates.TIMING, (ios_release.POLL_SECONDS, ios_release.WAIT_MINUTES))
        self.assertEqual((print_gates.TRUE_VALUES, print_gates.FALSE_VALUES),
                         (ios_release.TRUE_VALUES, ios_release.FALSE_VALUES))


class RedactLogsTests(unittest.TestCase):
    SECRETS = {"ASC_KEY_ID": "AB1C2D3E4F", "ASC_ISSUER_ID": "69a6de7f-0000-4000-8000-123456789abc"}

    def tree(self, tmp: str) -> Path:
        root = Path(tmp) / "logs"
        (root / "dist.xcdistributionlogs").mkdir(parents=True)
        key, issuer = self.SECRETS["ASC_KEY_ID"], self.SECRETS["ASC_ISSUER_ID"]
        (root / "archive.log").write_text(f"-authenticationKeyID {key} -authenticationKeyIssuerID {issuer}\nAB1XC2\n")
        (root / "dist.xcdistributionlogs" / "binary.plist").write_bytes(b"bplist00\x00\x01" + key.encode() + b"\x00")
        (root / "dist.xcdistributionlogs" / "le.txt").write_bytes(f"key={key}".encode("utf-16-le"))
        (root / "dist.xcdistributionlogs" / "be.txt").write_bytes(("\ufeff" + f"issuer={issuer}").encode("utf-16-be"))
        return root

    def run_cli(self, root: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ, env if env is not None else self.SECRETS), \
                redirect_stdout(out), redirect_stderr(out):
            code = redact_logs.main([str(root), "ASC_KEY_ID", "ASC_ISSUER_ID"])
        return code, out.getvalue()

    def everything(self, root: Path) -> bytes:
        return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())

    def test_every_file_and_encoding_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            code, output = self.run_cli(root)
            self.assertEqual(code, 0, output)
            self.assertEqual(
                (root / "archive.log").read_text(),
                "-authenticationKeyID <ASC_KEY_ID> -authenticationKeyIssuerID <ASC_ISSUER_ID>\nAB1XC2\n",
            )
            self.assertIn(b"<ASC_KEY_ID>", (root / "dist.xcdistributionlogs" / "binary.plist").read_bytes())
            self.assertEqual((root / "dist.xcdistributionlogs" / "le.txt").read_bytes().decode("utf-16-le"),
                             "key=<ASC_KEY_ID>")
            self.assertEqual((root / "dist.xcdistributionlogs" / "be.txt").read_bytes().decode("utf-16-be"),
                             "\ufeffissuer=<ASC_ISSUER_ID>")
            self.assertFalse(redact_logs.leaks(self.everything(root), self.SECRETS))

    def test_links_and_unredactable_files_are_removed_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            outside = Path(tmp) / "AuthKey.p8"
            outside.write_text(f"secret key for {self.SECRETS['ASC_KEY_ID']}")
            (root / "key-link").symlink_to(outside)
            (root / "dir-link").symlink_to(Path(tmp), target_is_directory=True)
            locked = root / "locked.log"
            locked.write_text(self.SECRETS["ASC_KEY_ID"])
            with mock.patch.object(redact_logs.Path, "write_bytes", side_effect=PermissionError("read-only")):
                code, output = self.run_cli(root)
            self.assertEqual(code, 0, output)
            self.assertFalse((root / "key-link").is_symlink())
            self.assertFalse((root / "dir-link").is_symlink())
            self.assertFalse(locked.exists())  # could not be rewritten, so it was removed
            self.assertIn(self.SECRETS["ASC_KEY_ID"], outside.read_text())  # a link target is never touched
            self.assertFalse(redact_logs.leaks(self.everything(root), self.SECRETS))

    def test_fails_when_a_dirty_file_cannot_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            with mock.patch.object(redact_logs.Path, "write_bytes", side_effect=PermissionError("read-only")), \
                    mock.patch.object(redact_logs, "remove", return_value=False):
                code, output = self.run_cli(root)
            self.assertEqual(code, 1)
            self.assertIn("must not be uploaded", output)

    def test_unset_ids_and_missing_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            before = self.everything(root)
            code, _ = self.run_cli(root, env={"ASC_KEY_ID": "", "ASC_ISSUER_ID": ""})
            self.assertEqual((code, self.everything(root)), (0, before))
            self.assertEqual(self.run_cli(Path(tmp) / "absent")[0], 0)


class ExportListingTests(unittest.TestCase):
    def test_writes_fields_and_removes_cleared_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loc = Path(tmp)
            (loc / "whats_new.txt").write_text("old")
            written = export_listing.write_fields(
                loc, {"description": "Desc\n", "keywords": "", "whatsNew": None},
                asc_release.LISTING_FIELDS + (asc_release.WHATS_NEW_FIELD,))
            self.assertEqual(written, ["description.txt"])
            self.assertEqual((loc / "description.txt").read_text(), "Desc\n")
            self.assertFalse((loc / "whats_new.txt").exists())

    def test_picks_the_live_version_unless_asked(self) -> None:
        versions = [
            {"id": "A", "attributes": {"versionString": "1.0", "appStoreState": "READY_FOR_SALE"}},
            {"id": "B", "attributes": {"versionString": "1.0.1", "appStoreState": "PREPARE_FOR_SUBMISSION"}},
        ]
        self.assertEqual(export_listing.pick_version(versions, None)["id"], "A")
        self.assertEqual(export_listing.pick_version(versions, "1.0.1")["id"], "B")
        with self.assertRaises(SystemExit):
            quiet(export_listing.pick_version, versions, "2.0")


# Stand-ins for `xcrun simctl` and `sleep` in IosTestScriptTests (no simulator).
# Each `simctl list devices --json` reports the next state from fake.json's
# "reads" (the last one repeats; null means simctl does not list the simulator),
# `simctl shutdown SIM` exits with "shutdown_exit", and every call is recorded.
FAKE_XCRUN = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path

state_file = Path(__file__).with_name("fake.json")
fake = json.loads(state_file.read_text())
args = sys.argv[1:]
fake["calls"].append(" ".join(args))
code = 0
if args == ["simctl", "list", "devices", "--json"]:
    state = fake["reads"].pop(0) if len(fake["reads"]) > 1 else fake["reads"][0]
    devices = [{"udid": "OTHER", "name": "A developer's iPhone", "state": "Booted"}]
    if state is not None:
        devices.append({"udid": "SIM", "name": "ABYSM CI", "state": state})
    print(json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": devices}}))
elif args == ["simctl", "shutdown", "SIM"]:
    code = fake["shutdown_exit"]
else:
    code = 64
state_file.write_text(json.dumps(fake))
sys.exit(code)
'''
FAKE_SLEEP = '#!/bin/sh\necho "$1" >> "$(dirname "$0")/sleeps"\n'


class IosTestScriptTests(unittest.TestCase):
    """scripts/test-ios.sh: the simulator is shut down, and confirmed shut down, between the suites."""

    SCRIPT = (ROOT / "scripts" / "test-ios.sh").read_text()

    def shut_down(self, reads: list[str | None], *, shutdown_exit: int = 0) -> tuple[subprocess.CompletedProcess,
                                                                                    list[str], list[str]]:
        """Run the script's shut_down_simulator() under its own shell options against the fakes."""
        functions = [re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", self.SCRIPT, re.M | re.S)
                     for name in ("simulator_state", "shut_down_simulator")]
        self.assertTrue(all(functions), "simulator_state() and shut_down_simulator() in scripts/test-ios.sh")
        with tempfile.TemporaryDirectory() as tmp:
            fakes = Path(tmp)
            (fakes / "fake.json").write_text(json.dumps({"reads": reads, "shutdown_exit": shutdown_exit, "calls": []}))
            for name, body in (("xcrun", FAKE_XCRUN), ("sleep", FAKE_SLEEP)):
                (fakes / name).write_text(body)
                (fakes / name).chmod(0o755)
            harness = "\n".join([
                re.search(r"^set -euo pipefail$", self.SCRIPT, re.M).group(0),
                "simulator_id=SIM",
                *(function.group(0) for function in functions),
                "if shut_down_simulator; then echo SHUT_DOWN; else echo NOT_SHUT_DOWN; fi",
            ])
            result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False,
                                    env=dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}"))
            calls = json.loads((fakes / "fake.json").read_text())["calls"]
            sleeps = (fakes / "sleeps").read_text().split() if (fakes / "sleeps").exists() else []
        return result, calls, sleeps

    def test_the_ui_tests_start_only_after_the_simulator_is_shut_down(self) -> None:
        self.assertIn("run_bundle AbysmTests UnitTests || status=1\n\nshut_down_simulator || exit 1\n\n"
                      "run_bundle AbysmUITests UITests || status=1\n", self.SCRIPT)
        # The old check listed the simulator's launchd jobs, which fails once the
        # simulator is shut down, and read that failure as "the app is gone".
        self.assertNotIn("launchctl", self.SCRIPT)

    def test_a_booted_or_shutting_down_simulator_is_waited_for_until_shut_down(self) -> None:
        # Read before the shutdown, then polled: still going down once, then shut down.
        for before, shutdown_exit in (("Booted", 0), ("Shutting Down", 149)):
            with self.subTest(before=before):
                result, calls, sleeps = self.shut_down([before, before, "Shutdown"], shutdown_exit=shutdown_exit)
                self.assertEqual((result.returncode, result.stdout.splitlines()[-1]), (0, "SHUT_DOWN"), result.stderr)
                self.assertIn(f"state after the unit tests: {before}", result.stdout)
                self.assertIn("simctl shutdown SIM", calls)
                self.assertEqual(sleeps, ["0.5"])

    def test_a_simulator_xcodebuild_already_shut_down_is_ready_at_once(self) -> None:
        # simctl refuses to shut down a shut-down simulator (exit 149); that is fine.
        result, calls, sleeps = self.shut_down(["Shutdown"], shutdown_exit=149)
        self.assertEqual((result.returncode, result.stdout.splitlines()[-1]), (0, "SHUT_DOWN"), result.stderr)
        self.assertIn("state after the unit tests: Shutdown", result.stdout)
        self.assertEqual(sleeps, [])

    def test_a_simulator_that_does_not_shut_down_stops_the_run(self) -> None:
        result, _, sleeps = self.shut_down(["Booted"])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "NOT_SHUT_DOWN"), result.stderr)
        self.assertIn("still Booted, not shut down; not starting the UI tests", result.stderr)
        self.assertEqual(len(sleeps), 60)

    def test_a_simulator_simctl_does_not_list_stops_the_run(self) -> None:
        result, calls, _ = self.shut_down([None])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "NOT_SHUT_DOWN"), result.stderr)
        self.assertIn("simctl lists 0 simulators with id SIM", result.stderr)
        self.assertNotIn("simctl shutdown SIM", calls)


class WorkflowWiringTests(unittest.TestCase):
    WORKFLOW = (ROOT / ".github" / "workflows" / "ios-release.yml").read_text()

    def test_every_switch_reaches_the_gates_step(self) -> None:
        for name, _ in ios_release.OPERATION_SWITCHES + ((ios_release.MASTER, None),):
            self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", self.WORKFLOW, name)
        for name in (ios_release.POLL_SECONDS[0], ios_release.WAIT_MINUTES[0]):
            self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", self.WORKFLOW, name)

    def test_only_tag_runs_upload_and_validation_never_does(self) -> None:
        self.assertIn("if: env.MODE == 'release'", self.WORKFLOW)
        self.assertIn('tags:\n      - "v*"', self.WORKFLOW)
        self.assertNotIn("pull_request", self.WORKFLOW)

    def job(self, name: str) -> str:
        """Text of one top-level job, up to the next job (jobs are indented two spaces)."""
        match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", self.WORKFLOW, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_tag_runs_queue_in_one_group(self) -> None:
        # Releases run one at a time, a third run never cancels a pending one,
        # and each validation run keeps its own group.
        self.assertIn(
            "group: ios-release-${{ github.event_name == 'push' && 'release' || github.run_id }}",
            self.WORKFLOW,
        )
        self.assertIn("cancel-in-progress: false", self.WORKFLOW)
        self.assertRegex(self.WORKFLOW, r"(?m)^  queue: max$")

    def test_build_wait_is_its_own_job(self) -> None:
        wait = self.job("wait-for-build")
        self.assertIn("needs: [quality, prepare, build]", wait)
        self.assertIn("needs.prepare.outputs.upload_build == 'true'", wait)
        self.assertIn("needs.build.result == 'success'", wait)
        self.assertIn("build_id: ${{ steps.wait.outputs.build_id }}", wait)
        self.assertIn("ios_release.py wait-build", wait)
        timeout = int(re.search(r"timeout-minutes: (\d+)", wait).group(1))
        self.assertGreater(timeout, ios_release.WAIT_MINUTES[3])
        self.assertEqual(self.WORKFLOW.count("ios_release.py wait-build"), 1)

        store = self.job("app-store-connect")
        self.assertIn("needs: [quality, prepare, build, wait-for-build]", store)
        self.assertIn("BUILD_ID: ${{ needs.wait-for-build.outputs.build_id }}", store)
        self.assertIn("needs.prepare.outputs.asc == 'true'", store)
        self.assertNotIn("steps.wait", store)

        release = self.job("github-release")
        self.assertIn("needs: [quality, prepare, build, wait-for-build, app-store-connect]", release)
        self.assertIn(
            "(needs.wait-for-build.result == 'success' || needs.wait-for-build.result == 'skipped')",
            release,
        )

    def step(self, job: str, name: str) -> str:
        """Text of one step of a job, from its `- name:` line to the next step."""
        body = self.job(job)
        start = body.index(f"      - name: {name}\n")
        end = body.find("\n      - ", start + 1)
        return body[start:] if end == -1 else body[start:end]

    def test_build_logs_are_redacted_before_upload(self) -> None:
        build = self.job("build")
        self.assertIn("name: ios-release-logs-${{ github.run_id }}-${{ github.run_attempt }}", build)
        self.assertIn("/scripts/store/redact_logs.py", build)  # in the sparse checkout
        collect = self.step("build", "Collect logs")
        self.assertIn("id: collect-logs", collect)
        self.assertIn('python3 scripts/store/redact_logs.py "$RUNNER_TEMP/logs" ASC_KEY_ID ASC_ISSUER_ID', collect)
        self.assertNotIn("|| true", collect.split("redact_logs.py", 1)[1])  # a failed redaction fails the step
        keep = self.step("build", "Keep build logs")
        # Nothing is uploaded unless redaction succeeded.
        self.assertIn("if: failure() && steps.collect-logs.outcome == 'success'", keep)
        self.assertLess(build.index("redact_logs.py \"$RUNNER_TEMP/logs\""), build.index("actions/upload-artifact"))
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            (logs / "archive.log").write_text("-authenticationKeyID AB1.C2 -authenticationKeyIssuerID 69a6-de7f\nAB1XC2\n")
            (logs / "dist.bin").write_bytes(b"\x00\x01AB1.C2\x00" + "69a6-de7f".encode("utf-16-le"))
            env = dict(os.environ, ASC_KEY_ID="AB1.C2", ASC_ISSUER_ID="69a6-de7f")
            subprocess.run([sys.executable, str(HERE / "redact_logs.py"), str(logs), "ASC_KEY_ID", "ASC_ISSUER_ID"],
                           env=env, check=True, capture_output=True)
            self.assertEqual(
                (logs / "archive.log").read_text(),
                "-authenticationKeyID <ASC_KEY_ID> -authenticationKeyIssuerID <ASC_ISSUER_ID>\nAB1XC2\n",
            )
            self.assertEqual((logs / "dist.bin").read_bytes(),
                             b"\x00\x01<ASC_KEY_ID>\x00" + "<ASC_ISSUER_ID>".encode("utf-16-le"))

    def test_app_store_version_preflight_runs_before_the_build_on_tag_runs_only(self) -> None:
        prepare = self.job("prepare")
        preflight = self.step("prepare", "Check the App Store version (read-only preflight)")
        # asc is only ever on for a tag run (validation runs resolve every switch off).
        self.assertIn("if: steps.gates.outputs.asc == 'true'", preflight)
        self.assertIn('python3 -u scripts/store/asc_release.py --preflight --version "$MARKETING_VERSION"', preflight)
        self.assertIn("MARKETING_VERSION: ${{ steps.gates.outputs.marketing_version }}", preflight)
        for flag, output in (("UPLOAD_LISTING", "listing"), ("UPLOAD_WHATS_NEW", "whats_new"),
                             ("UPLOAD_SCREENSHOTS", "screenshots"), ("UPLOAD_CREATIVE_ASSETS", "creative"),
                             ("SUBMIT_IOS_REVIEW", "submit"), ("PUBLIC_RELEASE", "public_release")):
            self.assertIn(f"{flag}: ${{{{ steps.gates.outputs.{output} }}}}", preflight)
        self.assertNotIn("--build-id", preflight)
        install = prepare.index("      - name: Install store tooling\n")
        self.assertIn("if: >- steps.gates.outputs.build == 'true' || steps.gates.outputs.asc == 'true' || "
                      "steps.gates.outputs.create_version == 'true'",
                      " ".join(self.step("prepare", "Install store tooling").split()))
        self.assertLess(install, prepare.index("Check the App Store version (read-only preflight)"))
        self.assertLess(prepare.index("--preflight"), prepare.index("ios_release.py build-number"))
        self.assertIn("needs.prepare.result == 'success'", self.job("build"))  # the build waits for it
        self.assertEqual(gates({}, event="workflow_dispatch", ref="main")["asc"], False)

    def test_app_store_version_is_created_before_the_preflight_on_tag_runs_only(self) -> None:
        prepare = self.job("prepare")
        create = self.step("prepare", "Create the App Store version if it is missing (IOS_CREATE_VERSION)")
        self.assertIn("if: steps.gates.outputs.mode == 'release' && steps.gates.outputs.create_version == 'true'", create)
        self.assertIn('python3 -u scripts/store/asc_release.py --create-version --version "$MARKETING_VERSION"', create)
        self.assertIn("CREATE_IOS_VERSION: ${{ steps.gates.outputs.create_version }}", create)
        self.assertIn("MARKETING_VERSION: ${{ steps.gates.outputs.marketing_version }}", create)
        for name in ("APP_STORE_CONNECT_API_KEY_ID", "APP_STORE_CONNECT_ISSUER_ID", "APP_STORE_CONNECT_API_KEY_P8"):
            self.assertIn(f"{name}: ${{{{ secrets.{name} }}}}", create)
        self.assertIn("create_version: ${{ steps.gates.outputs.create_version }}", prepare)
        self.assertEqual(self.WORKFLOW.count("--create-version"), 1)
        # After the offline checks and the tooling install, before the preflight and any build.
        position = prepare.index("--create-version")
        self.assertLess(prepare.index("asc_release.py --dry-run"), position)
        self.assertLess(prepare.index("      - name: Install store tooling\n"), position)
        self.assertLess(position, prepare.index("asc_release.py --preflight"))
        self.assertLess(position, prepare.index("ios_release.py build-number"))
        self.assertIn("steps.gates.outputs.create_version == 'true'", self.step("prepare", "Install store tooling"))
        # Validation runs resolve it off whatever the switches say.
        self.assertFalse(gates(ALL_ON, event="workflow_dispatch", ref="main")["create_version"])

    def test_export_compliance_is_checked_before_the_build_and_in_the_archive(self) -> None:
        build = self.job("build")
        verify = self.step("build", "Verify app identity and version in every bundle")
        command = 'python3 scripts/store/ios_release.py export-compliance --info-plist "$main/Info.plist"'
        self.assertIn(command, verify)
        self.assertIn("/scripts/store/ios_release.py", build)  # in the build job's sparse checkout
        # After the archive, before anything is exported, validated or uploaded.
        self.assertLess(build.index("      - name: Archive Release\n"), build.index(command))
        self.assertLess(build.index(command), build.index("      - name: Write export options\n"))
        # The gates step reads the project's answer before anything is built, so
        # prepare (and the pull request checks) need the app's Info.plist.
        self.assertIn("/Abysm/Info.plist", self.job("prepare"))
        checks = (ROOT / ".github" / "workflows" / "store-checks.yml").read_text()
        self.assertIn("            /Abysm/Info.plist\n", checks)
        self.assertEqual(checks.count("      - 'Abysm/Info.plist'\n"), 2)

    def test_unused_store_assets_never_block_a_release(self) -> None:
        offline = self.step("prepare", "Check the release tooling and inputs (offline)")
        self.assertIn("STORE_TESTS_SCOPE: release", offline)
        # The listing/screenshot checks a release does run are the gated dry run's.
        self.assertIn("UPLOAD_LISTING: ${{ steps.gates.outputs.listing }}", offline)
        self.assertIn("UPLOAD_SCREENSHOTS: ${{ steps.gates.outputs.screenshots }}", offline)
        self.assertIn("UPLOAD_CREATIVE_ASSETS: ${{ steps.gates.outputs.creative }}", offline)
        checks = (ROOT / ".github" / "workflows" / "store-checks.yml").read_text()
        self.assertNotIn("STORE_TESTS_SCOPE", checks)  # PRs and main check every asset
        env = {k: v for k, v in os.environ.items() if k != "STORE_TESTS_SCOPE"}
        tests = [f"test_ios_release.{name}" for name in REPO_CONTENT_TESTS + ("CatalogTests.test_repo_catalog_matches_the_app",)]
        for scope, skipped in (("release", len(REPO_CONTENT_TESTS)), ("", 0)):
            result = subprocess.run([sys.executable, "-m", "unittest", "-v", *tests], cwd=HERE, capture_output=True,
                                    text=True, env=dict(env, STORE_TESTS_SCOPE=scope), check=False)
            with self.subTest(scope=scope):
                self.assertEqual(result.stderr.count("skipped 'release run"), skipped, result.stderr)
                # The catalog check always runs: it covers the products the build sells.
                # (Pass or fail of the real-file checks is reported by the normal run.)
                self.assertRegex(result.stderr, r"test_repo_catalog_matches_the_app .*\.\.\. (ok|FAIL)")

    def test_the_store_step_gets_the_creative_switch(self) -> None:
        self.assertIn("creative: ${{ steps.gates.outputs.creative }}", self.job("prepare"))
        store = self.step("app-store-connect", "Listing / What's New / screenshots / creative assets / review (gated)")
        self.assertIn("UPLOAD_CREATIVE_ASSETS: ${{ needs.prepare.outputs.creative }}", store)
        # Creative images wait up to 10 min each for processing, on top of the screenshots.
        timeout = int(re.search(r"timeout-minutes: (\d+)", self.job("app-store-connect")).group(1))
        self.assertGreaterEqual(timeout, 60)
        checks = (ROOT / ".github" / "workflows" / "store-checks.yml").read_text()
        for workflow in (self.WORKFLOW, checks):
            self.assertIn("IOS_UPLOAD_CREATIVE_ASSETS: ${{ vars.IOS_UPLOAD_CREATIVE_ASSETS }}", workflow)
        self.assertIn('UPLOAD_CREATIVE_ASSETS: "true"', checks)  # the dry run checks the committed images

    def test_pillow_is_installed_wherever_screenshots_are_staged_and_tested(self) -> None:
        # Pinned to one version and hash-locked (the lock file is in every store job's
        # checkout). Any version: Dependabot's weekly store-tooling bump must pass here.
        lock = (ROOT / "requirements-store.txt").read_text()
        self.assertRegex(lock, r"(?m)^pillow==[0-9]+(\.[0-9]+)+ \\\n    --hash=sha256:[0-9a-f]{64}")
        install = "python3 -m pip install --disable-pip-version-check --require-hashes -r requirements-store.txt"
        # prepare stages the screenshots in its gated dry run and preflight, after the install.
        prepare = self.job("prepare")
        self.assertLess(prepare.index(install), prepare.index("      - name: Check the release tooling and inputs (offline)\n"))
        self.assertLess(prepare.index(install), prepare.index("asc_release.py --preflight"))
        self.assertIn(install, self.job("app-store-connect"))
        # The Store release checks install it before the tests, which must not skip the RGB copies there.
        checks = (ROOT / ".github" / "workflows" / "store-checks.yml").read_text()
        self.assertLess(checks.index(install), checks.index("python3 -m unittest discover"))
        self.assertLess(checks.index(install), checks.index("asc_release.py --dry-run"))
        self.assertIn('STORE_TESTS_NEED_PILLOW: "1"', checks)

    def test_store_tooling_uses_a_pinned_python_never_the_system_one(self) -> None:
        for workflow in ("ios-release.yml", "store-checks.yml"):
            text = (ROOT / ".github" / "workflows" / workflow).read_text()
            for match in re.finditer(r"^  ([A-Za-z0-9_-]+):\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text.split("\njobs:\n", 1)[1],
                                     re.M | re.S):
                job, body = match.group(1), match.group(2)
                if "pip install" not in body:
                    continue
                with self.subTest(workflow=workflow, job=job):
                    setup = body.find("actions/setup-python@")
                    self.assertGreater(setup, -1)
                    self.assertLess(setup, body.index("pip install"))
                    self.assertRegex(body, r"actions/setup-python@[0-9a-f]{40} # v\d")
                    self.assertIn('python-version: "3.13"', body)

    def test_store_steps_run_unbuffered(self) -> None:
        for command in ("ios_release.py build-number", "ios_release.py wait-build", "asc_release.py --preflight",
                        "asc_release.py --create-version", 'asc_release.py "${args[@]}"'):
            with self.subTest(command=command):
                self.assertIn(f"python3 -u scripts/store/{command}", self.WORKFLOW)
                self.assertNotIn(f"python3 scripts/store/{command}", self.WORKFLOW)

    def test_job_outputs_reach_the_build_wait_through_env(self) -> None:
        wait = self.step("wait-for-build", "Wait for App Store Connect to process this build")
        script = wait.split("run: |\n", 1)[1]
        self.assertNotIn("${{", script)
        for name, output in (("BUILD_NUMBER", "needs.prepare.outputs.build_number"),
                             ("UPLOAD_STARTED_AT", "needs.build.outputs.upload_started_at"),
                             ("WAIT_MINUTES", "needs.prepare.outputs.wait_minutes"),
                             ("POLL_SECONDS", "needs.prepare.outputs.poll_seconds")):
            self.assertIn(f"{name}: ${{{{ {output} }}}}", wait)
            self.assertIn(f'"${name}"', script)

    def test_no_workflow_uses_the_node20_cache_action(self) -> None:
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=workflow.name):
                self.assertNotIn("actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830", workflow.read_text())

    def test_release_notes_never_block_a_release_that_does_not_use_them(self) -> None:
        # What's New is checked before the build, only when its switch is on.
        prepare = self.job("prepare")
        self.assertIn("UPLOAD_WHATS_NEW: ${{ steps.gates.outputs.whats_new }}", prepare)
        self.assertLess(prepare.index("asc_release.py --dry-run"), prepare.index("ios_release.py build-number"))
        # A tag without a RELEASE_NOTES.md section still gets a GitHub Release.
        self.assertIn("3) notes=(--generate-notes) ;;", self.job("github-release"))

    def test_release_notes_are_checked_before_the_build(self) -> None:
        prepare = self.job("prepare")
        start = prepare.index("      - name: Check release notes\n")
        end = prepare.find("\n      - ", start + 1)
        step = prepare[start:] if end == -1 else prepare[start:end]
        self.assertIn("if: github.event_name == 'push'", step)
        self.assertIn("TAG: ${{ github.ref_name }}", step)
        # Before App Store Connect is asked for a build number, so before any build.
        self.assertLess(start, prepare.index("ios_release.py build-number"))
        script = textwrap.dedent(step.split("run: |\n", 1)[1])

        def check(notes: str | None) -> int:
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "scripts").mkdir()
                shutil.copy(ROOT / "scripts" / "release_notes.py", Path(tmp) / "scripts")
                if notes is not None:
                    (Path(tmp) / "RELEASE_NOTES.md").write_text(notes)
                result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp, capture_output=True,
                                        text=True, env=dict(os.environ, TAG=TAG), check=False)
                return result.returncode

        self.assertEqual(check(f"## {TAG}\n\n- Fixes\n"), 0)
        self.assertEqual(check("## Unreleased\n\n- next\n"), 0)  # no section: generated notes
        self.assertEqual(check(None), 0)  # no RELEASE_NOTES.md at all
        self.assertEqual(check(f"## {TAG}\n\n## v1.0\n\n- old\n"), 1)  # empty section fails early
        self.assertEqual(check(f"## {TAG}\n\n- Fixes\n\n## {TAG}\n\n- Other\n"), 1)  # so does a duplicate

    def test_old_release_workflow_is_gone(self) -> None:
        self.assertFalse((ROOT / ".github" / "workflows" / "github-release.yml").exists())

    def test_scripts_run_from_the_command_line(self) -> None:
        for script in ("ios_release.py", "asc_release.py", "export_listing.py", "print_gates.py", "validate_catalog.py",
                       "redact_logs.py"):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(HERE / script), "--help"],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
