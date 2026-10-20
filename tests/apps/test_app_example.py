"""End to end through the CLI, against the example app: other tag prefixes, titles and asset names,
two App Store sets, no catalog. Proves nothing Fud AI-specific is hard-coded."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store_release import __version__, callers, config
from support import apps as support_apps
from support import clirun, trees

support_apps.configure_fud_ai_defaults()

SHA = "b3" * 20


class ExampleAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.root = trees.write_example_tree(cls.base / "example")
        cfg = config.load_for_app(cls.root)
        for platform, name in callers.CALLER_FILES.items():
            (cls.root / ".github/workflows" / name).write_text(callers.render(
                cfg, platform, sha=SHA, version=__version__, app_repository="someone/example"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_cli(self, *args: str, **env: str):
        return clirun.run(self.root, *args, env=clirun.clean_env(**env))

    def test_config_and_paths(self) -> None:
        result = self.run_cli("config", "check", "--files")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("paths", "--job", "ios-build")
        self.assertEqual(result.stdout.splitlines(), ["/store/store-release.toml", "/Example.xcodeproj/project.pbxproj",
                                                      "/Example/", "/Example.xcodeproj/"])

    def test_gates_use_the_example_prefixes(self) -> None:
        out = self.base / "gates.out"
        out.write_text("")
        result = self.run_cli("ios", "gates", GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="v1.0", GITHUB_OUTPUT=str(out),
                              IOS_RELEASE_AUTOMATION="true", IOS_UPLOAD_LISTING="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(clirun.outputs(out)["listing"], "true")
        result = self.run_cli("ios", "gates", GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="ios-v1.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("release runs need an v* tag", result.stderr)
        result = self.run_cli("android", "gates", GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="android-v1.0",
                              ANDROID_RELEASE_AUTOMATION="true")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_catalog_and_the_gate(self) -> None:
        result = self.run_cli("catalog", "check")
        self.assertEqual((result.returncode, result.stdout.strip()),
                         (0, "catalog: no [catalog] in store-release.toml; nothing to check"))
        result = self.run_cli("revenuecat-gate", STORE_SYNC_REVENUECAT="true")
        self.assertEqual(result.returncode, 1)

    def test_staging_and_the_store_step_without_duo_or_watch(self) -> None:
        staged = self.base / "staged"
        result = self.run_cli("screenshots", "stage", "--platform", "all", "--out", str(staged))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.parent.relative_to(staged).as_posix() for p in staged.rglob("*.png")),
                         ["ios/6.7", "ios/6.7", "ios/ipad-13", "ios/ipad-13", "play/phone", "play/phone"])
        meta = self.base / "meta"
        result = self.run_cli("listing", "prepare", "--out", str(meta))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("ios", "store-step", "--dry-run", "--metadata-dir", str(meta), UPLOAD_LISTING="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bundle=com.example.app", result.stdout)
        result = self.run_cli("ios", "store-step", "--dry-run", "--bundle-id", "com.apoorvdarshan.calorietracker",
                              UPLOAD_LISTING="true")
        self.assertIn("only supports the Example iOS bundle (com.example.app)", result.stderr)

    def test_release_titles_and_asset_names(self) -> None:
        gh = clirun.fake_gh(self.base / "gh")
        result = self.run_cli("github", "release", "--platform", "ios", "--tag", "v1.0", "--version", "1.0",
                              "--uploaded-build", "", "--notes-file", "", GITHUB_REPOSITORY="someone/example", **gh)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(clirun.gh_calls(gh)[-1][-4:], ["--title", "Example 1.0", "--generate-notes", "--latest"])
        build = self.root / "android/app/build/outputs"
        for folder, name in (("bundle/release", "app-release.aab"), ("apk/release", "app-release.apk")):
            (build / folder).mkdir(parents=True, exist_ok=True)
            (build / folder / name).write_bytes(name.encode())
        result = self.run_cli("github", "stage-android-assets", "--tag", "android-v1.0", "--out", str(self.base / "dist"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in (self.base / "dist").iterdir()),
                         ["example-android-1.0-3.aab", "example-android-1.0-3.apk"])

    def test_callers_check(self) -> None:
        env = dict(TOOL_SHA=SHA, TOOL_REPOSITORY="apoorvdarshan/store-release", GITHUB_REPOSITORY="someone/example")
        result = self.run_cli("caller", "check", "--pr", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("caller", "check", "--runtime", "--platform", "ios", **env,
                              GITHUB_WORKFLOW_REF="someone/example/.github/workflows/ios-release.yml@refs/tags/v1.0")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
