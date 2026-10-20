"""iOS additions: archive verification, export options, the SPM cache key, read-only App Store Connect."""
from __future__ import annotations

import argparse
import io
import os
import plistlib
import subprocess
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release import __version__
from store_release.ios import archive
from store_release.ios import asc as asc_release
from store_release.ios import release as ios_release
from support import apps as support_apps

support_apps.configure_fud_ai_defaults()

BUNDLE = "com.apoorvdarshan.calorietracker"
XCODE = "Xcode 27.0\nBuild version 27A100\n"


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def bundle(path: Path, identifier: str, version: str = "7.2", build: str = "40", **extra) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": identifier, "CFBundleShortVersionString": version, "CFBundleVersion": build, **extra},
        fmt=plistlib.FMT_BINARY))


class ArchiveVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.archive = Path(tmp.name) / "fudai.xcarchive"
        apps = self.archive / "Products" / "Applications"
        self.main = apps / "Fud AI.app"
        bundle(self.main, BUNDLE, ITSAppUsesNonExemptEncryption=False)
        bundle(self.main / "PlugIns" / "Widgets.appex", BUNDLE + ".widgets")
        bundle(self.main / "Watch" / "FudAIWatchApp.app", BUNDLE + ".watchkitapp")
        bundle(self.main / "Watch" / "FudAIWatchApp.app" / "PlugIns" / "WatchWidgets.appex", BUNDLE + ".ww")
        bundle(self.main / "PlugIns" / "Share.appex", BUNDLE + ".share")
        # Frameworks never count, even when they hold an .app-shaped bundle.
        bundle(self.main / "Frameworks" / "Lib.framework" / "Helper.app", "com.vendor.helper", "1.0", "1")

    def test_every_app_and_extension_carries_the_version(self) -> None:
        plist, checked = archive.verify_archive(self.archive, bundle_id=BUNDLE, version="7.2", build="40")
        self.assertEqual(checked, 5)
        self.assertEqual(plist, str(self.main / "Info.plist"))

    def test_a_nested_extension_with_another_build_fails(self) -> None:
        bundle(self.main / "Watch" / "FudAIWatchApp.app" / "PlugIns" / "WatchWidgets.appex", BUNDLE + ".ww", build="39")
        with self.assertRaisesRegex(archive.ArchiveError,
                                    r"Fud AI.app/Watch/FudAIWatchApp.app/PlugIns/WatchWidgets.appex is 7.2 \(39\), "
                                    r"expected 7.2 \(40\)"):
            archive.verify_archive(self.archive, bundle_id=BUNDLE, version="7.2", build="40")

    def test_versions_compare_as_plistbuddy_prints_them(self) -> None:
        # fud-ai compared PlistBuddy's text, so an <integer> build number matched its decimal string.
        with self.main.joinpath("Info.plist").open("rb") as handle:
            plist = plistlib.load(handle)
        plist["CFBundleVersion"] = 40
        (self.main / "Info.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        self.assertEqual(archive.verify_archive(self.archive, bundle_id=BUNDLE, version="7.2", build="40")[1], 5)
        # Any other type, or a missing key, fails and says why instead of printing the same value twice.
        plist["CFBundleVersion"] = 40.0
        (self.main / "Info.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        with self.assertRaisesRegex(archive.ArchiveError, r"Fud AI.app is 7.2 \(40.0 \(a float, not a string\)\), "
                                                          r"expected 7.2 \(40\)"):
            archive.verify_archive(self.archive, bundle_id=BUNDLE, version="7.2", build="40")
        plist["CFBundleVersion"] = True
        (self.main / "Info.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        with self.assertRaisesRegex(archive.ArchiveError, r"\(True \(a bool, not a string\)\)"):
            archive.verify_archive(self.archive, bundle_id=BUNDLE, version="7.2", build="40")
        del plist["CFBundleVersion"]
        (self.main / "Info.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        with self.assertRaisesRegex(archive.ArchiveError, r"Fud AI.app is 7.2 \(missing\), expected 7.2 \(40\)"):
            archive.verify_archive(self.archive, bundle_id=BUNDLE, version="7.2", build="40")

    def test_identity_and_missing_app(self) -> None:
        with self.assertRaisesRegex(archive.ArchiveError, f"archived {BUNDLE}, expected com.example.app"):
            archive.verify_archive(self.archive, bundle_id="com.example.app", version="7.2", build="40")
        with self.assertRaisesRegex(archive.ArchiveError, "no .app under Products/Applications"):
            archive.verify_archive(self.archive.parent / "empty.xcarchive", bundle_id=BUNDLE, version="7.2", build="40")

    def test_the_command_checks_export_compliance_after(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            archive.main(["verify-archive", "--archive", str(self.archive), "--version", "7.2", "--build", "40"],
                         app_root=self.archive.parent, developer_dir="/x", patterns=(), bundle_id=BUNDLE, team_id="X")
        self.assertIn(f"verified {BUNDLE} 7.2 (40) in 5 app/extension bundles", out.getvalue())
        self.assertIn("export compliance: ITSAppUsesNonExemptEncryption = NO", out.getvalue())
        bundle(self.main, BUNDLE)  # no declaration
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            archive.main(["verify-archive", "--archive", str(self.archive), "--version", "7.2", "--build", "40"],
                         app_root=self.archive.parent, developer_dir="/x", patterns=(), bundle_id=BUNDLE, team_id="X")
        self.assertIn("does not declare export compliance", err.getvalue())


class ExportOptionsTests(unittest.TestCase):
    def test_per_mode(self) -> None:
        for mode, destination in (("release", "upload"), ("validate", "export")):
            options = plistlib.loads(archive.export_options(mode=mode, team_id="23RV7FYH36"))
            with self.subTest(mode=mode):
                self.assertEqual(options, {"method": "app-store-connect", "teamID": "23RV7FYH36",
                                           "signingStyle": "automatic", "destination": destination,
                                           "manageAppVersionAndBuildNumber": False, "uploadSymbols": True})
        with self.assertRaises(archive.ArchiveError):
            archive.export_options(mode="upload", team_id="X")


class SpmCacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "ios/calorietracker.xcodeproj/project.xcworkspace/xcshareddata/swiftpm").mkdir(parents=True)
        self.resolved = self.root / "ios/calorietracker.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
        self.resolved.write_text('{"pins": []}')
        (self.root / "ios/Packages/LiteRTLM").mkdir(parents=True)
        (self.root / "ios/Packages/LiteRTLM/Package.swift").write_text("// swift-tools-version:5.9\n")
        self.patterns = ("ios/**/Package.resolved", "ios/Packages/**/Package.swift")

    def digest(self, developer_dir: str = "/Applications/Xcode_27.app/Contents/Developer") -> str:
        return archive.spm_digest(self.root, developer_dir, self.patterns)

    def test_the_key_moves_with_xcode_and_the_files_only(self) -> None:
        first = self.digest()
        self.assertEqual(first, self.digest(), "stable for the same inputs")
        self.assertEqual(len(first), 40)
        self.assertNotEqual(first, self.digest("/Applications/Xcode_27.1.app/Contents/Developer"))
        self.resolved.write_text('{"pins": [1]}')
        self.assertNotEqual(first, self.digest())
        self.assertEqual(archive.package_files(self.root, self.patterns),
                         ["ios/Packages/LiteRTLM/Package.swift",
                          "ios/calorietracker.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"])
        key = archive.spm_key(runner_os="macOS", runner_arch="ARM64", xcode_id="xcode27.0-27A100", digest=first)
        self.assertEqual(key, f"ios-release-spm-macOS-ARM64-xcode27.0-27A100-{first}")

    def test_xcode_identity(self) -> None:
        self.assertEqual(archive.xcode_identity(XCODE), "xcode27.0-27A100")
        self.assertEqual(archive.xcode_identity("Xcode 26.6\nBuild version 17G5\n"), "xcode26.6-17G5")
        with self.assertRaises(archive.ArchiveError):
            archive.xcode_identity("xcodebuild: error")

    def test_select_xcode_checks_exports_and_writes_the_key(self) -> None:
        developer = self.root / "Xcode_27.app/Contents/Developer"
        developer.mkdir(parents=True)
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs["env"]["DEVELOPER_DIR"]))
            return subprocess.CompletedProcess(argv, 0, XCODE, "")

        with tempfile.NamedTemporaryFile("r", suffix=".env") as env_file, \
                tempfile.NamedTemporaryFile("r", suffix=".out") as out_file:
            environ = {"GITHUB_ENV": env_file.name, "GITHUB_OUTPUT": out_file.name, "RUNNER_OS": "macOS",
                       "RUNNER_ARCH": "ARM64"}
            key = quiet(archive.select_xcode, self.root, str(developer), self.patterns, environ=environ, run=run)
            self.assertEqual(Path(env_file.name).read_text(), f"DEVELOPER_DIR={developer}\n")
            self.assertEqual(Path(out_file.name).read_text(), f"spm_key={key}\n")
        self.assertEqual(calls, [(["xcodebuild", "-version"], str(developer))])
        self.assertTrue(key.startswith("ios-release-spm-macOS-ARM64-xcode27.0-27A100-"))
        with self.assertRaisesRegex(archive.ArchiveError, "released Xcode"):
            archive.select_xcode(self.root, "/Applications/Xcode-beta.app/Contents/Developer", self.patterns, run=run)
        with self.assertRaisesRegex(archive.ArchiveError, "does not exist"):
            archive.select_xcode(self.root, str(self.root / "missing"), self.patterns, run=run)


class FakeHttp:
    """urlopen stand-in that records requests and answers GETs with an empty collection."""

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return io.BytesIO(b'{"data": []}')


class ReadOnlyClientTests(unittest.TestCase):
    def client(self) -> asc_release.ReadOnlyClient:
        return asc_release.ReadOnlyClient(token="t")

    def test_writes_never_leave_the_runner(self) -> None:
        http = FakeHttp()
        with mock.patch.object(asc_release.urllib.request, "urlopen", http):
            client = self.client()
            self.assertEqual(client.get("/apps?filter%5BbundleId%5D=x"), {"data": []})
            for call in (lambda: client.post("/appStoreVersions", {}), lambda: client.patch("/apps/1", {}),
                         lambda: client.request("DELETE", "/appScreenshots/1"),
                         lambda: client.request("put", "/x", body={}),
                         lambda: client.upload_bytes("https://upload.example/x?sig=1", b"x", {})):
                with self.assertRaises(asc_release.AscHttpError) as caught:
                    call()
                self.assertIn("read-only client refused", str(caught.exception.code))
                self.assertEqual(caught.exception.status, 0)
        self.assertEqual([request.get_method() for request in http.requests], ["GET"])
        self.assertFalse(ios_release.is_transient_error(asc_release.AscHttpError(0, "refused")))

    def test_client_from_environment_read_only(self) -> None:
        env = {"APP_STORE_CONNECT_API_KEY_ID": "K", "APP_STORE_CONNECT_ISSUER_ID": "I",
               "APP_STORE_CONNECT_API_KEY_P8": "P"}
        with mock.patch.dict(os.environ, env), mock.patch.object(asc_release, "make_asc_token", return_value="jwt"):
            self.assertIsInstance(asc_release.client_from_environment(read_only=True), asc_release.ReadOnlyClient)
            client = asc_release.client_from_environment()
            self.assertIs(type(client), asc_release.AscClient)

    def test_read_only_commands_ask_for_a_read_only_client(self) -> None:
        """D17: build-number, version-preflight and wait-build only read."""
        calls = []

        def fake_asc_client(*, read_only=False):
            calls.append(read_only)
            raise ios_release.ReleaseError("stop here")

        env = {"SUBMIT_IOS_REVIEW": "true"}
        with mock.patch.object(ios_release, "asc_client", side_effect=fake_asc_client), \
                mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
            for run in (lambda: ios_release.cmd_build_number(argparse.Namespace()),
                        lambda: ios_release.cmd_version_preflight(argparse.Namespace(version="7.2")),
                        lambda: ios_release.cmd_wait_build(argparse.Namespace(
                            version="7.2", build=40, started_at=1.8e9, wait_minutes=120, poll_seconds=30))):
                with self.assertRaises(ios_release.ReleaseError):
                    run()
        self.assertEqual(calls, [True, True, True])


class ProjectCommandTests(unittest.TestCase):
    def test_project_export_compliance_and_versions(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            ios_release.main(["export-compliance", "--project"])
            ios_release.main(["project-version", "--setting", "MARKETING_VERSION"])
            ios_release.main(["project-version", "--setting", "CURRENT_PROJECT_VERSION"])
        self.assertIn("ITSAppUsesNonExemptEncryption = NO (no non-exempt encryption) in every calorietracker "
                      "configuration", out.getvalue())
        self.assertIn("MARKETING_VERSION = 7.2 in every configuration", out.getvalue())
        self.assertIn("CURRENT_PROJECT_VERSION = 39 in every configuration", out.getvalue())
        with mock.patch.object(ios_release, "single_project_value", return_value="7.x"), \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            ios_release.main(["project-version", "--setting", "MARKETING_VERSION"])
        self.assertIn("is not a valid value", err.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_:
            ios_release.main(["export-compliance"])
        self.assertEqual(exit_.exception.code, 2)

    def test_the_bundle_guard_names_the_configured_app(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("UPLOAD_", "SUBMIT_", "PUBLIC_"))}
        with mock.patch.dict(os.environ, {**env, "UPLOAD_LISTING": "true"}, clear=True), \
                mock.patch.object(asc_release.sys, "argv", ["store-step", "--dry-run", "--bundle-id", "x.y"]), \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            asc_release.main()
        self.assertIn(f"this publisher only supports the Fud AI iOS bundle ({BUNDLE})", err.getvalue())

    def test_dry_run_self_check_uses_neutral_ids(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            asc_release.dry_run_self_check()
        self.assertIn("product submission selection OK", out.getvalue())


if __name__ == "__main__":
    unittest.main()


# --- the read-only store plan (store_release.ios.plan) ---------------------------------------

import json as _json  # noqa: E402
import subprocess as _subprocess  # noqa: E402

from store_release.ios import plan as ios_plan  # noqa: E402
from support.asc_fakes import image_item, version_item  # noqa: E402
from support.plan_fakes import PlanAsc  # noqa: E402

PLAN_ENV = {"IOS_RELEASE_AUTOMATION": "true"}


def gh_runner(tags=("ios-v7.1.2",), existing=()):
    def run(argv):
        argv = list(argv)
        if argv[:3] == ["gh", "release", "list"]:
            return _subprocess.CompletedProcess(argv, 0, _json.dumps([{"tagName": t} for t in tags]), "")
        if argv[:3] == ["gh", "release", "view"]:
            return _subprocess.CompletedProcess(argv, 0 if argv[3] in existing else 1, "", "")
        raise AssertionError(f"a store plan ran {argv}")
    return run


class PlanClientOverlayTests(unittest.TestCase):
    """Each overlay effect: writes are recorded and answered, later reads see them, nothing is sent."""

    class Transport:
        def __init__(self, responses: dict) -> None:
            self.responses = responses
            self.paths: list[str] = []

        def get(self, path: str) -> dict:
            self.paths.append(path)
            for prefix, payload in self.responses.items():
                if path.startswith(prefix):
                    return _json.loads(_json.dumps(payload))
            return {"data": []}

    def client(self, **responses) -> ios_plan.PlanClient:
        self.transport = self.Transport({
            "/apps/APP1/appStoreVersions": {"data": [
                {"id": "V0", "attributes": {"versionString": "7.1.2", "appStoreState": "READY_FOR_SALE"}},
                {"id": "V9", "attributes": {"versionString": "7.1.10", "appStoreState": "REPLACED_WITH_NEW_VERSION"}}]},
            "/appStoreVersions/V9/appStoreVersionLocalizations": {"data": [{"id": "LOC9", "attributes": {
                "locale": "en-US", "description": "Old"}}]},
            **responses})
        return ios_plan.PlanClient(self.transport, version_string="7.2", build_number="41")

    def test_a_created_version_reads_as_a_copy_of_the_newest(self) -> None:
        client = self.client()
        client.post("/appStoreVersions", {"data": {"attributes": {"platform": "IOS", "versionString": "7.2"}}})
        versions = client.get("/apps/APP1/appStoreVersions?limit=50")["data"]
        self.assertEqual([v["id"] for v in versions], ["V0", "V9", ios_plan.PLAN_VERSION_ID])
        rows = client.get(f"/appStoreVersions/{ios_plan.PLAN_VERSION_ID}/appStoreVersionLocalizations?limit=1")["data"]
        self.assertEqual(rows[0]["id"], "LOC9", "7.1.10 is newer than 7.1.2")
        self.assertTrue(client.approximate)
        self.assertEqual(client.writes[0]["summary"], "{platform: IOS, versionString: 7.2}")

    def test_listing_patch_is_merged_into_later_reads(self) -> None:
        client = self.client()
        client.get("/appStoreVersions/V9/appStoreVersionLocalizations?x")
        client.patch("/appStoreVersionLocalizations/LOC9", {"data": {"attributes": {"description": "New text"}}})
        row = client.get("/appStoreVersions/V9/appStoreVersionLocalizations?x")["data"][0]
        self.assertEqual(row["attributes"]["description"], "New text")
        self.assertEqual(client.writes[0]["summary"], "description 3 → 8 chars")

    def test_screenshot_sets_and_screenshots(self) -> None:
        client = self.client(**{"/appStoreVersionLocalizations/LOC9/appScreenshotSets": {"data": [
            {"id": "SET1", "attributes": {"screenshotDisplayType": "APP_IPHONE_67"}}]},
            "/appScreenshotSets/SET1/appScreenshots": {"data": [{"id": "SHOT1", "attributes": {"fileName": "old.png"}}]}})
        self.assertEqual(len(client.get("/appStoreVersionLocalizations/LOC9/appScreenshotSets")["data"]), 1)
        created = client.post("/appScreenshotSets", {"data": {"attributes": {"screenshotDisplayType": "APP_IPHONE_DUO"},
                                                              "relationships": {"appStoreVersionLocalization": {
                                                                  "data": {"id": "LOC9"}}}}})["data"]
        sets = client.get("/appStoreVersionLocalizations/LOC9/appScreenshotSets")["data"]
        self.assertEqual([s["id"] for s in sets], ["SET1", created["id"]])
        shot = client.post("/appScreenshots", {"data": {"attributes": {"fileName": "01.png", "fileSize": 4},
                                                        "relationships": {"appScreenshotSet": {"data": {"id": "SET1"}}}}})
        self.assertEqual(shot["data"]["attributes"]["uploadOperations"][0]["url"], f"plan://upload/{shot['data']['id']}")
        client.request("DELETE", "/appScreenshots/SHOT1")
        listed = [row["id"] for row in client.get("/appScreenshotSets/SET1/appScreenshots")["data"]]
        self.assertEqual(listed, [shot["data"]["id"]])
        self.assertEqual(client.get(f"/appScreenshotSets/{created['id']}/appScreenshots")["data"], [])
        client.upload_bytes(f"plan://upload/{shot['data']['id']}", b"data", {})
        with self.assertRaises(asc_release.AscHttpError):
            client.upload_bytes("https://upload.example/real", b"data", {})

    def test_library_images_drafts_items_builds_and_placements(self) -> None:
        client = self.client(**{"/appAssetLibraries/LIB/images": {"data": [{"id": "OLD", "attributes": {"state": "APPROVED"}}]},
                                "/apps/APP1/reviewSubmissions": {"data": [{"id": "REAL"}]},
                                "/appStoreVersionLocalizations/LOC9/placements": {"data": [
                                    {"id": "PL-OLD", "attributes": {"placementType": "PRODUCT_PAGE_HEADER_ASSET"}}]}})
        image = client.post("/appAssetLibraryImages", {"data": {"attributes": {"fileName": "h.png", "fileSize": 9},
                                                                "relationships": {"assetLibrary": {"data": {"id": "LIB"}}}}})
        image_id = image["data"]["id"]
        self.assertEqual([i["id"] for i in client.get("/appAssetLibraries/LIB/images?x")["data"]], ["OLD", image_id])
        client.patch(f"/appAssetLibraryImages/{image_id}", {"data": {"attributes": {"uploaded": True}}})
        processed = client.get(f"/appAssetLibraryImages/{image_id}")["data"]["attributes"]
        self.assertEqual((processed["state"], processed["imageAsset"]), ("PREPARE_FOR_SUBMISSION", {"width": 1, "height": 1}))
        draft = client.post("/reviewSubmissions", {"data": {}})["data"]["id"]
        drafts = client.get("/apps/APP1/reviewSubmissions?filter%5Bstate%5D=READY_FOR_REVIEW&limit=5")["data"]
        self.assertEqual([d["id"] for d in drafts], ["REAL", draft])
        client.post("/reviewSubmissionItems", {"data": {"relationships": {
            "reviewSubmission": {"data": {"id": draft}}, "appAssetLibraryImage": {"data": {"id": image_id}}}}})
        self.assertEqual(len(client.get(f"/reviewSubmissions/{draft}/items?include=x")["data"]), 1)
        self.assertEqual(client.get(f"/appAssetLibraryImages/{image_id}")["data"]["attributes"]["state"], "READY_FOR_REVIEW")
        client.request("PATCH", f"/reviewSubmissions/{draft}", body={"data": {"attributes": {"submitted": True}}})
        drafts = client.get("/apps/APP1/reviewSubmissions?filter%5Bstate%5D=READY_FOR_REVIEW&limit=5")["data"]
        self.assertEqual([d["id"] for d in drafts], ["REAL"])
        client.request("PATCH", "/appStoreVersions/V9/relationships/build", body={"data": {"type": "builds", "id": "plan-build"}})
        self.assertEqual(client.get("/appStoreVersions/V9/build")["data"]["id"], "plan-build")
        build = client.get("/builds/plan-build?include=preReleaseVersion")
        self.assertEqual((build["data"]["attributes"]["processingState"], build["included"][0]["attributes"]["version"]),
                         ("VALID", "7.2"))
        client.post("/appAssetLibraryPlacements", {"data": {
            "attributes": {"placementType": "PRODUCT_PAGE_HEADER_ASSET", "placementGroup": "DEFAULT_PROFILE"},
            "relationships": {"image": {"data": {"id": "OLD"}}, "appStoreVersionLocalization": {"data": {"id": "LOC9"}},
                              "placementToSwapOut": {"data": {"id": "PL-OLD"}}}}})
        slot = client.get("/appStoreVersionLocalizations/LOC9/placements?filter%5BplacementType%5D=PRODUCT_PAGE_HEADER_ASSET")
        self.assertEqual(len(slot["data"]), 1)
        self.assertNotEqual(slot["data"][0]["id"], "PL-OLD")
        self.assertEqual(len(client.get("/appAssetLibraryImages/OLD/relationships/placements?limit=50")["data"]), 1)
        client.post("/somethingNew", {"data": {"type": "x"}})
        self.assertEqual(client.writes[-1]["kind"], "unrecognised")
        self.assertFalse([p for p in self.transport.paths if "plan-" in p], "virtual ids are never sent")


class StorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.app = support_apps.default_app()
        configured = support_apps.configured(self.app)
        configured.__enter__()
        self.addCleanup(configured.__exit__, None, None, None)
        for patch in (mock.patch.object(asc_release.time, "sleep"),
                      # The fakes' app record (APP1) is this app's App Store Connect app here.
                      mock.patch.object(asc_release, "ASC_APP_ID", "APP1")):
            patch.start()
            self.addCleanup(patch.stop)

    def plan(self, switches: dict, fake: PlanAsc, *, gh=None) -> tuple[int, str, dict]:
        env = {**PLAN_ENV, **switches, "GITHUB_REPOSITORY": "apoorvdarshan/fud-ai"}
        out = io.StringIO()
        with redirect_stdout(out):
            code, result = ios_plan.run_plan(self.app, version="7.2", work_dir=self.tmp / "plan", environ=env,
                                             transport=fake, gh_runner=gh or gh_runner())
        self.assertEqual(set(fake.methods), {"GET"} if fake.methods else set(), "only GETs reach App Store Connect")
        return code, out.getvalue(), result

    def test_the_spec_example(self) -> None:
        fake = PlanAsc(versions=[("7.1.2", "READY_FOR_SALE")], builds=["13", "40"])
        code, out, result = self.plan({"IOS_CREATE_VERSION": "true", "IOS_UPLOAD_BUILD": "true"}, fake)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"store-plan ios: Fud AI 7.2 (tag ios-v7.2), store-release v{__version__}")
        self.assertEqual(lines[1], "switches a tag run would use: master=on create_version=on upload_build=on "
                                   "listing=off whats_new=off screenshots=off creative=off submit=off public_release=off")
        self.assertIn("1. create-version: App Store version 7.2 is missing → WOULD CREATE (POST /appStoreVersions)", lines)
        self.assertIn("   refusal checks passed: 7.1.2 READY_FOR_SALE; 7.2 is newer than every version", lines)
        self.assertIn("2. build: next build number 41 (project 39, highest uploaded 40); xcodebuild uploads the binary",
                      lines)
        self.assertIn("3. store step: not planned (no listing/What's New/screenshot/creative/review switch on)", lines)
        self.assertIn("4. GitHub Release: create the release ios-v7.2 titled 'iOS 7.2 (build 41)' with --latest", lines)
        self.assertIn("  POST /appStoreVersions  create the App Store version: {platform: IOS, versionString: 7.2}", lines)
        self.assertEqual(lines[-2:], ["problems a tag run would hit: none", "store-plan: CLEAN"])
        saved = _json.loads((self.tmp / "plan" / "store-plan.json").read_text())
        self.assertEqual(saved["status"], "store-plan: CLEAN")
        self.assertEqual([w["path"] for w in saved["writes"]], ["/appStoreVersions"])

    def test_the_header_names_the_store_release_commit_only(self) -> None:
        # In a called workflow GITHUB_WORKFLOW_SHA is the caller's (the app's) commit: never shown as store-release's.
        fakes = (PlanAsc(versions=[("7.1.2", "READY_FOR_SALE")], builds=["40"]) for _ in range(2))
        _, out, _ = self.plan({"GITHUB_WORKFLOW_SHA": "a" * 40}, next(fakes))
        self.assertEqual(out.splitlines()[0], f"store-plan ios: Fud AI 7.2 (tag ios-v7.2), store-release v{__version__}")
        _, out, _ = self.plan({"GITHUB_WORKFLOW_SHA": "a" * 40, "TOOL_SHA": "b" * 40}, next(fakes))
        self.assertEqual(out.splitlines()[0],
                         f"store-plan ios: Fud AI 7.2 (tag ios-v7.2), store-release v{__version__} ({'b' * 40})")

    def test_a_missing_version_without_create_is_a_problem(self) -> None:
        fake = PlanAsc(versions=[("7.1.2", "READY_FOR_SALE")])
        code, out, result = self.plan({"IOS_UPLOAD_LISTING": "true"}, fake)
        self.assertEqual(code, 0)
        self.assertIn("preflight: refused: no IOS App Store version '7.2'", out)
        self.assertIn("store step: not planned (blocked by problem 1)", out)
        self.assertEqual(out.splitlines()[-1], "store-plan: 1 PROBLEM(S)")
        self.assertIn("::warning title=store-plan::preflight: no IOS App Store version", out)

    def test_create_refusals_are_problems(self) -> None:
        fake = PlanAsc(versions=[("7.1.2", "READY_FOR_SALE"), ("7.1.3", "IN_REVIEW")])
        code, out, _ = self.plan({"IOS_CREATE_VERSION": "true", "IOS_UPLOAD_LISTING": "true"}, fake)
        self.assertEqual(code, 0)
        self.assertIn("create-version: refused: not creating App Store version 7.2", out)
        self.assertIn("preflight: not planned (blocked by problem 1)", out)

    def test_a_fresh_editable_version_with_review(self) -> None:
        fake = PlanAsc(live_text={"description": "Old"}, iaps=[{"id": "IAP1", "attributes": {
            "name": "Credits 50", "productId": "com.apoorvdarshan.calorietracker.credits.50", "state": "READY_TO_SUBMIT"}}])
        switches = {name: "true" for name in ("IOS_UPLOAD_BUILD", "IOS_UPLOAD_LISTING", "IOS_UPLOAD_WHATS_NEW",
                                              "IOS_SUBMIT_REVIEW", "IOS_PUBLIC_RELEASE")}
        code, out, result = self.plan(switches, fake)
        self.assertEqual(code, 0, out)
        writes = [(w["method"], w["path"]) for w in result["writes"]]
        self.assertEqual(writes, [
            ("PATCH", "/appStoreVersionLocalizations/LOC1"),
            ("PATCH", "/appStoreVersions/V1/relationships/build"),
            ("PATCH", "/appStoreVersions/V1"),
            ("POST", "/reviewSubmissions"),
            ("POST", "/inAppPurchaseSubmissions"),
            ("POST", "/reviewSubmissionItems"),
            ("PATCH", "/reviewSubmissions/plan-draft-1"),
        ])
        self.assertIn("create a draft review submission: would create plan-draft-1", out)
        self.assertIn("set releaseType AFTER_APPROVAL", out)
        self.assertIn("build selection simulated", out)
        self.assertIn("store-plan: CLEAN", out)

    def test_ready_for_review_with_and_without_its_draft(self) -> None:
        good = PlanAsc(state="READY_FOR_REVIEW", drafts=["MINE"], draft_items={"MINE": [version_item()]})
        switches = {"IOS_UPLOAD_BUILD": "true", "IOS_SUBMIT_REVIEW": "true"}
        code, out, result = self.plan(switches, good)
        self.assertIn("store-plan: CLEAN", out)
        self.assertIn(("PATCH", "/reviewSubmissions/MINE"), [(w["method"], w["path"]) for w in result["writes"]])
        bad = PlanAsc(state="READY_FOR_REVIEW", drafts=["MIXED"], draft_items={"MIXED": [version_item(), image_item("X")]})
        code, out, _ = self.plan(switches, bad)
        self.assertEqual(code, 0)
        self.assertIn("no draft review submission without header or search results images holds it", out)

    def test_waiting_for_review_stops_at_the_preflight(self) -> None:
        for live_text in ({"description": "A synthetic description.\n\nIt has two paragraphs."}, {"description": "Old"}):
            fake = PlanAsc(state="WAITING_FOR_REVIEW", attached="B9", live_text=live_text)
            code, out, _ = self.plan({"IOS_UPLOAD_LISTING": "true"}, fake)
            with self.subTest(live_text=live_text):
                self.assertEqual(code, 0)
                self.assertIn("App Store version 7.2 is WAITING_FOR_REVIEW", out)

    def test_creative_states(self) -> None:
        creative = asc_release.DEFAULT_CREATIVE_DIR
        header = next((creative / "header").glob("*.png"))
        search = next((creative / "search-results").glob("*.png"))

        def image(image_id, path, state):
            return {"id": image_id, "attributes": {"fileName": path.name, "fileSize": path.stat().st_size,
                                                   "state": state, "createdDate": "2026-10-20"}}

        placed = {"PL-H": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "LOC1"},
                  "PL-S": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "S", "loc": "LOC1"}}
        cases = [
            ("new images", PlanAsc(), ["/appAssetLibraryImages", "/reviewSubmissions", "/reviewSubmissionItems"], "CLEAN"),
            ("approved and placed", PlanAsc(library=[image("H", header, "APPROVED"), image("S", search, "APPROVED")],
                                            placements=placed), [], "CLEAN"),
            ("approved, not placed", PlanAsc(library=[image("H", header, "APPROVED"), image("S", search, "APPROVED")]),
             ["/appAssetLibraryPlacements"], "CLEAN"),
            ("in review", PlanAsc(library=[image("H", header, "IN_REVIEW"), image("S", search, "IN_REVIEW")]), [], "CLEAN"),
            ("rejected", PlanAsc(library=[image("H", header, "REJECTED"), image("S", search, "APPROVED")]), None,
             "PROBLEM"),
        ]
        for label, fake, paths, status in cases:
            code, out, result = self.plan({"IOS_UPLOAD_CREATIVE_ASSETS": "true"}, fake)
            with self.subTest(label=label):
                self.assertEqual(code, 0)
                self.assertIn(status, out.splitlines()[-1])
                if paths is not None:
                    found = [w["path"] for w in result["writes"] if w["method"] != "UPLOAD"]
                    for path in paths:
                        self.assertIn(path, found)
                    if not paths:
                        self.assertEqual(found, [])

    def test_master_off_plans_only_the_github_release(self) -> None:
        fake = PlanAsc()
        code, out, result = self.plan({"IOS_RELEASE_AUTOMATION": "false", "IOS_UPLOAD_BUILD": "true"}, fake)
        self.assertEqual(code, 0)
        self.assertIn("a tag run would not touch App Store Connect", out)
        self.assertIn("titled 'iOS 7.2 (build 39)'", out)
        self.assertEqual(fake.methods, [])

    def test_invalid_switches_and_the_github_latest_rule(self) -> None:
        code, out, _ = self.plan({"IOS_SUBMIT_REVIEW": "maybe"}, PlanAsc())
        self.assertEqual(code, 0)
        self.assertIn("IOS_SUBMIT_REVIEW='maybe' is not a valid switch value", out)
        self.assertIn("1 PROBLEM(S)", out)
        code, out, _ = self.plan({"IOS_RELEASE_AUTOMATION": "false"}, PlanAsc(),
                                 gh=gh_runner(tags=("ios-v7.3",), existing=("ios-v7.2",)))
        self.assertIn("update the existing release ios-v7.2 titled 'iOS 7.2 (build 39)' with --latest=false", out)

    def test_exit_codes(self) -> None:
        class Unauthorized:
            def get(self, path):
                raise asc_release.AscHttpError(401, f"ASC GET {path} → HTTP 401")

        with redirect_stdout(io.StringIO()), self.assertRaises(ios_plan.PlanAbort):
            ios_plan.run_plan(self.app, version="7.2", work_dir=self.tmp / "p", environ={**PLAN_ENV,
                              "IOS_UPLOAD_LISTING": "true"}, transport=Unauthorized(), gh_runner=gh_runner())
        env = {k: v for k, v in os.environ.items() if not k.startswith("APP_STORE_CONNECT_")}
        app = mock.Mock(config=self.app.config, root=self.app.root)
        with mock.patch.dict(os.environ, {**env, **PLAN_ENV, "IOS_UPLOAD_LISTING": "true"}, clear=True), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ios_plan.main(app, ["--version", "7.2", "--work-dir", str(self.tmp / "q")]), 1)
        self.assertIn("missing ASC credentials", err.getvalue())
        with mock.patch.object(ios_plan, "run_plan", side_effect=KeyError("boom")), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ios_plan.main(app, ["--version", "7.2", "--work-dir", str(self.tmp / "r")]), 1)
        self.assertIn("unexpected error", err.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ios_plan.main(app, ["--version", "seven", "--work-dir", str(self.tmp / "s")]), 1)
