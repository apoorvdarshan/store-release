"""End to end through the CLI, against a synthetic app shaped like Fud AI (sizes and counts included)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store_release import __version__, callers, config
from store_release.ios import asc
from support import apps as support_apps
from support import clirun, trees
from support.play_fakes import FakePlay

support_apps.configure_fud_ai_defaults()

from support.asc_fakes import FakeAsc  # noqa: E402 - needs the configured modules

SHA = "a1" * 20
IOS_SWITCHES = ("IOS_CREATE_VERSION", "IOS_UPLOAD_BUILD", "IOS_UPLOAD_LISTING", "IOS_UPLOAD_WHATS_NEW",
                "IOS_UPLOAD_SCREENSHOTS", "IOS_UPLOAD_CREATIVE_ASSETS", "IOS_SUBMIT_REVIEW", "IOS_PUBLIC_RELEASE")
ANDROID_SWITCHES = ("ANDROID_UPLOAD_LISTING", "ANDROID_UPLOAD_WHATS_NEW", "ANDROID_UPLOAD_SCREENSHOTS",
                    "ANDROID_INTERNAL_TESTING", "ANDROID_CLOSED_TESTING", "ANDROID_PRODUCTION_ROLLOUT")


def png_info(path: Path) -> tuple[int, int, int]:
    raw = path.read_bytes()
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"), raw[25]


class FudAiAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.root = trees.write_fud_ai_tree(cls.base / "fud-ai", screenshots=True)
        cfg = config.load_for_app(cls.root)
        for platform, name in callers.CALLER_FILES.items():
            (cls.root / ".github/workflows" / name).write_text(callers.render(
                cfg, platform, sha=SHA, version=__version__, app_repository="apoorvdarshan/fud-ai"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_cli(self, *args: str, **env: str):
        return clirun.run(self.root, *args, env=clirun.clean_env(**env))

    def test_config_paths_and_export(self) -> None:
        result = self.run_cli("config", "check", "--files")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("every file and folder the config names exists", result.stdout)
        result = self.run_cli("paths", "--job", "ios-prepare", GITHUB_REPOSITORY="apoorvdarshan/fud-ai",
                              GITHUB_WORKFLOW_REF="apoorvdarshan/fud-ai/.github/workflows/ios-release.yml@refs/tags/ios-v7.2")
        self.assertEqual(result.stdout.splitlines()[:2], ["/store/store-release.toml", "/.github/workflows/ios-release.yml"])
        env_file = self.base / "github.env"
        env_file.write_text("")
        result = self.run_cli("config", "export", "--job", "android-publish", GITHUB_ENV=str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = clirun.outputs(env_file)
        self.assertEqual(exported["STORE_RELEASE_ANDROID_PACKAGE"], "com.apoorvdarshan.calorietracker")
        self.assertEqual(exported["STORE_RELEASE_SLUG"], "fudai")

    def gates(self, platform: str, event: str, ref: str, env: dict[str, str]) -> tuple[int, dict, str]:
        out = self.base / f"{platform}-gates.out"
        out.write_text("")
        result = self.run_cli(platform, "gates", GITHUB_EVENT_NAME=event, GITHUB_REF_NAME=ref, GITHUB_OUTPUT=str(out),
                              **env)
        return result.returncode, clirun.outputs(out), result.stdout + result.stderr

    def test_ios_gates_matrix(self) -> None:
        for event, ref in (("push", "ios-v7.2"), ("workflow_dispatch", "main")):
            for master in ("true", "false"):
                for switch in IOS_SWITCHES:
                    env = {"IOS_RELEASE_AUTOMATION": master, switch: "true"}
                    if switch == "IOS_SUBMIT_REVIEW":
                        env["IOS_UPLOAD_BUILD"] = "true"
                    if switch == "IOS_PUBLIC_RELEASE":
                        env.update(IOS_UPLOAD_BUILD="true", IOS_SUBMIT_REVIEW="true")
                    code, values, text = self.gates("ios", event, ref, env)
                    with self.subTest(event=event, master=master, switch=switch):
                        self.assertEqual(code, 0, text)
                        key = {"IOS_CREATE_VERSION": "create_version", "IOS_UPLOAD_BUILD": "upload_build",
                               "IOS_UPLOAD_LISTING": "listing", "IOS_UPLOAD_WHATS_NEW": "whats_new",
                               "IOS_UPLOAD_SCREENSHOTS": "screenshots", "IOS_UPLOAD_CREATIVE_ASSETS": "creative",
                               "IOS_SUBMIT_REVIEW": "submit", "IOS_PUBLIC_RELEASE": "public_release"}[switch]
                        on = event == "push" and master == "true"
                        self.assertEqual(values[key], "true" if on else "false")
                        self.assertEqual(values["mode"], "release" if event == "push" else "validate")
        for event, ref in (("push", "ios-v7.2"), ("workflow_dispatch", "main")):
            code, _, text = self.gates("ios", event, ref, {"IOS_RELEASE_AUTOMATION": "false", "IOS_UPLOAD_BUILD": "maybe"})
            with self.subTest(invalid=event):
                self.assertEqual(code, 1)
                self.assertIn("IOS_UPLOAD_BUILD='maybe' is not a valid switch value", text)
        code, _, text = self.gates("ios", "push", "ios-v7.3", {})
        self.assertEqual(code, 1)
        self.assertIn("does not match MARKETING_VERSION 7.2", text)

    def test_android_gates_matrix(self) -> None:
        for event, ref in (("push", "android-v7.2"), ("workflow_dispatch", "main")):
            for master in ("true", "false"):
                for switch in ANDROID_SWITCHES:
                    env = {"ANDROID_RELEASE_AUTOMATION": master, switch: "true", "ANDROID_INTERNAL_TESTING": "true"}
                    code, values, text = self.gates("android", event, ref, env)
                    with self.subTest(event=event, master=master, switch=switch):
                        self.assertEqual(code, 0, text)
                        published = event == "push" and master == "true"
                        self.assertEqual(values["build"], "true" if event == "workflow_dispatch" or published else "false")
                        self.assertEqual(values["github_release"], "true" if published else "false")
                        if published:
                            self.assertTrue(values["play_tracks"].startswith("internal"))
                        else:
                            self.assertEqual(values["play_tracks"], "")
        code, _, text = self.gates("android", "workflow_dispatch", "main", {"ANDROID_CLOSED_TESTING": "maybe"})
        self.assertEqual(code, 1)
        self.assertIn("ANDROID_CLOSED_TESTING='maybe'", text)
        code, _, text = self.gates("android", "push", "android-v7.1", {"ANDROID_RELEASE_AUTOMATION": "true"})
        self.assertEqual(code, 1)
        self.assertIn("does not match versionName 7.2", text)

    def test_catalog_check_with_the_hook(self) -> None:
        result = self.run_cli("catalog", "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok: 11 products (11 on Play)\nhook: scripts/ci/check_catalog.py\n", result.stdout)
        self.assertIn("ok: 11 products (11 on Play), 2 entitlements, 2 offerings", result.stdout)

    def test_text_and_staging_for_upload(self) -> None:
        meta = self.base / "meta"
        result = self.run_cli("listing", "prepare", "--platform", "all", "--out", str(meta))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("whats-new", "prepare", "--platform", "all", "--tag", "dry-run", "--out", str(meta))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.relative_to(meta).as_posix() for p in meta.rglob("*") if p.is_file()), [
            "ios/en-US/description.txt", "ios/en-US/keywords.txt", "ios/en-US/marketing_url.txt", "ios/en-US/name.txt",
            "ios/en-US/privacy_url.txt", "ios/en-US/promotional_text.txt", "ios/en-US/subtitle.txt",
            "ios/en-US/support_url.txt", "ios/en-US/whats_new.txt", "play/en-US/full_description.txt",
            "play/en-US/short_description.txt", "play/en-US/title.txt", "play/whatsnew-de-DE", "play/whatsnew-en-US"])
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("needs Pillow (not installed)")
        staged = self.base / "staged"
        result = self.run_cli("screenshots", "stage", "--platform", "all", "--out", str(staged),
                              STORE_UPLOAD_SCREENSHOTS="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        expect = {"ios/6.7": (10, (1290, 2796)), "ios/ipad-13": (7, (2064, 2752)), "ios/iphone-duo": (7, (1398, 2034)),
                  "ios/watch": (1, (416, 496)), "play/phone": (8, (1573, 2796))}
        for folder, (count, size) in expect.items():
            files = sorted((staged / folder).glob("*.png"))
            with self.subTest(folder=folder):
                self.assertEqual(len(files), count)
                for path in files:
                    width, height, colour = png_info(path)
                    self.assertEqual((width, height), size)
                    if folder.startswith("ios"):
                        self.assertEqual(colour, 2, "App Store screenshots are RGB without alpha")
        self.assertFalse((self.root / "store/metadata").exists())

        # The live store step against FakeAsc, over the staged sets (screenshots + listing).
        fake = FakeAsc()
        uploads = []
        env = clirun.clean_env(UPLOAD_SCREENSHOTS="true", UPLOAD_LISTING="true")
        with mock.patch.object(asc, "client_from_environment", return_value=fake), \
                mock.patch.object(asc, "upload_screenshot_file",
                                  lambda client, set_id, path: uploads.append((set_id, path.name))):
            code, out, err = clirun.in_process(["--app-root", str(self.root), "ios", "store-step", "--version", "7.2",
                                                "--metadata-dir", str(meta), "--screenshots-dir", str(staged / "ios")],
                                               env)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.created_sets, ["APP_IPHONE_67", "APP_IPAD_PRO_3GEN_129", "APP_IPHONE_DUO",
                                             "APP_WATCH_SERIES_10"])
        self.assertEqual(len(uploads), 25)
        self.assertIn(("PATCH", "/appStoreVersionLocalizations/LOC1"), fake.calls)

    def test_store_step_dry_runs(self) -> None:
        meta = self.base / "dry-meta"
        self.run_cli("listing", "prepare", "--platform", "ios", "--out", str(meta))
        self.run_cli("whats-new", "prepare", "--platform", "ios", "--tag", "dry-run", "--out", str(meta))
        result = self.run_cli("ios", "store-step", "--dry-run", "--metadata-dir", str(meta), "--build-id", "DRY-RUN",
                              "--build-number", "1", UPLOAD_LISTING="true", UPLOAD_WHATS_NEW="true",
                              SUBMIT_IOS_REVIEW="true", PUBLIC_RELEASE="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run self-check: product submission selection OK", result.stdout)
        result = self.run_cli("ios", "store-step", "--dry-run", UPLOAD_CREATIVE_ASSETS="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("creative=True", result.stdout)
        result = self.run_cli("ios", "store-step", "--create-version", "--dry-run", "--version", "7.2",
                              CREATE_IOS_VERSION="true")
        self.assertIn("would create App Store version 7.2", result.stdout)

    def test_android_preflights_against_fake_play(self) -> None:
        releases = self.base / "releases.json"
        releases.write_text(json.dumps([{"tagName": "android-v7.1.1", "name": "Android 7.1.1 (versionCode 41)"}]))
        result = self.run_cli("android", "github-preflight", "--releases", str(releases),
                              GITHUB_REF_NAME="android-v7.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        play = FakePlay(bundles=[40, 41], tracks={"internal": [(41, "completed")]})
        out = self.base / "play.out"
        out.write_text("")
        env = clirun.clean_env(PLAY_SERVICE_ACCOUNT_JSON="{}", GITHUB_RUN_ATTEMPT="1", GITHUB_OUTPUT=str(out))
        with mock.patch("store_release.android.play_listing.build_android_publisher", return_value=play):
            code, stdout, err = clirun.in_process(["--app-root", str(self.root), "android", "play-preflight",
                                                   "--tracks", "internal"], env)
        self.assertEqual(code, 0, err)
        self.assertEqual(clirun.outputs(out)["play_upload_needed"], "true")
        self.assertEqual(play.writes(), [], "the preflight only reads")
        self.assertIn(("edits.delete", {"packageName": "com.apoorvdarshan.calorietracker", "editId": "E1"}), play.calls)
        play = FakePlay(bundles=[42], tracks={"internal": [(42, "completed")], "alpha": [(41, "completed")]})
        with mock.patch("store_release.android.play_listing.build_android_publisher", return_value=play):
            code, stdout, err = clirun.in_process(["--app-root", str(self.root), "android", "check-tracks",
                                                   "--tracks", "internal,alpha"], env)
        self.assertEqual(code, 1)
        self.assertIn("closed testing (alpha) does not carry versionCode 42", err)
        self.assertEqual(play.writes(), [])

    def test_github_release_with_a_fake_gh(self) -> None:
        notes = self.base / "notes.md"
        notes.write_text("Shipped.\n")
        gh = clirun.fake_gh(self.base / "gh-ios", releases=[("ios-v7.1.2", "iOS 7.1.2 (build 39)")])
        result = self.run_cli("github", "release", "--platform", "ios", "--tag", "ios-v7.2", "--version", "7.2",
                              "--uploaded-build", "40", "--notes-file", str(notes),
                              GITHUB_REPOSITORY="apoorvdarshan/fud-ai", **gh)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(clirun.gh_calls(gh)[-1], ["release", "create", "ios-v7.2", "--repo", "apoorvdarshan/fud-ai",
                                                   "--verify-tag", "--title", "iOS 7.2 (build 40)", "--notes-file",
                                                   str(notes), "--latest"])
        gh = clirun.fake_gh(self.base / "gh-android", releases=[("android-v7.3", "Android 7.3 (versionCode 43)")],
                            existing=["android-v7.2"])
        result = self.run_cli("github", "release", "--platform", "android", "--tag", "android-v7.2", "--production",
                              "true", "--assets-dir", str(self.base / "dist"), "--notes-file", str(notes),
                              GITHUB_REPOSITORY="apoorvdarshan/fud-ai", **gh)
        self.assertEqual(result.returncode, 0, result.stderr)
        upload, edit = clirun.gh_calls(gh)[-2:]
        self.assertEqual(upload[:5], ["release", "upload", "android-v7.2", "--repo", "apoorvdarshan/fud-ai"])
        self.assertEqual(edit[-3:], ["--prerelease=false", "--latest=false", "--draft=false"])

    def test_callers_pins_and_runtime(self) -> None:
        env = dict(TOOL_SHA=SHA, TOOL_REPOSITORY="apoorvdarshan/store-release", GITHUB_REPOSITORY="apoorvdarshan/fud-ai")
        for args in (("caller", "check", "--pr"), ("pins", "check")):
            result = self.run_cli(*args, **env)
            with self.subTest(args=args):
                self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("caller", "check", "--runtime", "--platform", "android", **env,
                              GITHUB_WORKFLOW_REF="apoorvdarshan/fud-ai/.github/workflows/android-release.yml@refs/tags/x")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("pins", "check", **{**env, "TOOL_SHA": "b2" * 20})
        self.assertEqual(result.returncode, 1)

    def test_stage_checks_through_the_cli(self) -> None:
        good = ["stage", "check", "--stage", "ios-github-release", "--gates", "ios-tests=success quality=success",
                "--marketing-version", "7.2", "--uploaded-build", "40"]
        self.assertEqual(self.run_cli(*good, GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="ios-v7.2").returncode, 0)
        result = self.run_cli(*good[:-2], "--uploaded-build", "***", GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="ios-v7.2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("dropped", result.stderr)


if __name__ == "__main__":
    unittest.main()
