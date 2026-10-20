"""store-release's own building blocks: config, paths, switches, job-boundary checks, catalog,
hooks, GitHub Releases, yanked versions, release-note headings and the CLI. Stdlib only."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release import (__version__, callers, catalog, cli, config, github, hooks, paths, release_notes, stage,
                           switches, yanked)
from store_release.android import release as android_release
from store_release.ios import release as ios_release
from support import apps as support_apps
from support import pngs, trees
from support.tag_fakes import TagApi

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = support_apps.TOOL_ROOT
FUD_AI_TOML = TOOL_ROOT / "examples" / "fud-ai" / "store-release.toml"
EXAMPLE_TOML = TOOL_ROOT / "examples" / "example" / "store-release.toml"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def fud_ai_data() -> dict:
    return tomllib.loads(FUD_AI_TOML.read_text())


def walk(data, dotted: str):
    """(parent, key) for a dotted path like ios.gate_jobs[0].id."""
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", dotted)
    node = data
    for part in parts[:-1]:
        node = node[int(part[1:-1])] if part.startswith("[") else node[part]
    last = parts[-1]
    return node, int(last[1:-1]) if last.startswith("[") else last


def mutated(dotted: str, value) -> dict:
    data = fud_ai_data()
    parent, key = walk(data, dotted)
    parent[key] = value
    return data


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def run_cli(argv: list[str], env: dict | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("IOS_", "ANDROID_", "STORE_", "GITHUB_"))}
    environ.update(env or {})
    saved = support_apps.snapshot()
    try:
        with mock.patch.dict(os.environ, environ, clear=True), redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if not isinstance(exc.code, int) and exc.code:
                    err.write(str(exc.code))
    finally:
        support_apps.restore(saved)
    return code, out.getvalue(), err.getvalue()


class ConfigLoadTests(unittest.TestCase):
    def test_fud_ai_and_example_configs_load(self) -> None:
        fud_ai = config.load(FUD_AI_TOML)
        self.assertEqual((fud_ai.app.name, fud_ai.app.slug), ("Fud AI", "fudai"))
        self.assertEqual(fud_ai.ios.tag_prefix, "ios-v")
        self.assertEqual(fud_ai.ios.pbxproj, "ios/calorietracker.xcodeproj/project.pbxproj")
        self.assertEqual([s.display_type for s in fud_ai.ios.screenshot_sets],
                         ["APP_IPHONE_67", "APP_IPAD_PRO_3GEN_129", "APP_IPHONE_DUO", "WATCH"])
        self.assertEqual([job.id for job in fud_ai.ios.gate_jobs], ["ios-tests", "quality"])
        self.assertEqual([job.id for job in fud_ai.android.gate_jobs], ["quality"])
        self.assertEqual(fud_ai.android.build.tasks, (":app:bundleRelease", ":app:assembleRelease"))
        self.assertEqual(fud_ai.catalog.checks, ("scripts/ci/check_catalog.py",))
        example = config.load(EXAMPLE_TOML)
        self.assertIsNone(example.catalog)
        self.assertEqual(example.ios.title, "Example {version}")
        self.assertEqual(example.android.build.aab, "android/app/build/outputs/bundle/release/app-release.aab")
        self.assertEqual(example.android.build.keystore_properties, "android/keystore.properties")
        self.assertEqual(example.ios.whats_new.markdown, "APPSTORE.md")

    def test_defaults_apply_when_a_table_is_present_without_keys(self) -> None:
        data = fud_ai_data()
        data["ios"]["listing"] = {}
        data["ios"]["whats_new"] = {}
        data["ios"]["creative"] = {}
        data["android"]["whats_new"] = {}
        del data["release_notes"]
        parsed = config.parse(data)
        self.assertEqual(parsed.ios.listing, config.Listing("markdown", "APPSTORE.md"))
        self.assertEqual(parsed.ios.whats_new, config.WhatsNew("markdown", "APPSTORE.md", "What's New"))
        self.assertEqual(parsed.ios.creative_dir, "store/creative/ios")
        self.assertEqual(parsed.android.whats_new.markdown, "PLAYSTORE.md")
        self.assertEqual(parsed.release_notes, "RELEASE_NOTES.md")

    def test_missing_tables_stay_absent(self) -> None:
        data = fud_ai_data()
        for name in ("listing", "whats_new", "screenshots", "creative", "products", "github_release"):
            del data["ios"][name]
        for name in ("listing", "whats_new", "screenshots"):
            del data["android"][name]
        parsed = config.parse(data)
        self.assertIsNone(parsed.ios.listing)
        self.assertIsNone(parsed.ios.screenshot_sets)
        self.assertIsNone(parsed.ios.creative_dir)
        self.assertIsNone(parsed.ios.iap_route)
        self.assertEqual(parsed.ios.title, "iOS {version} (build {build})")
        self.assertIsNone(parsed.android.screenshots_dir)

    def test_at_least_one_platform_and_schema_1(self) -> None:
        data = fud_ai_data()
        del data["ios"], data["android"]
        with self.assertRaisesRegex(config.ConfigError, r"\[ios\] and \[android\]"):
            config.parse(data)
        with self.assertRaisesRegex(config.ConfigError, "^schema"):
            config.parse({**fud_ai_data(), "schema": 2})
        data = fud_ai_data()
        del data["schema"]
        with self.assertRaisesRegex(config.ConfigError, "schema: required"):
            config.parse(data)

    def test_invalid_toml_and_unreadable_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store-release.toml"
            path.write_text("schema = \n")
            with self.assertRaisesRegex(config.ConfigError, "not valid TOML"):
                config.load(path)
            with self.assertRaisesRegex(config.ConfigError, "cannot read"):
                config.load(Path(tmp) / "missing.toml")


class ConfigValueTests(unittest.TestCase):
    BAD = [
        ("schema", "1", "schema"),
        ("app.name", "", "app.name"),
        ("app.name", "x" * 65, "app.name"),
        ("app.name", "tab\tname", "app.name"),
        ("app.slug", "Fud AI", "app.slug"),
        ("release_notes.path", "/RELEASE_NOTES.md", "release_notes.path"),
        ("catalog.path", "../products.json", "catalog.path"),
        ("catalog.groups", [], "catalog.groups"),
        ("catalog.groups", ["tips", "tips"], "catalog.groups"),
        ("catalog.groups", ["bad-group"], "catalog.groups[0]"),
        ("catalog.id_prefix", "", "catalog.id_prefix"),
        ("catalog.play_rules", "true", "catalog.play_rules"),
        ("catalog.revenuecat", 5, "catalog.revenuecat"),
        ("catalog.checks", ["scripts/ci/check.rb"], "catalog.checks[0]"),
        ("checks.paths", ["../outside"], "checks.paths[0]"),
        ("checks.paths", ['has "quote"'], "checks.paths[0]"),
        ("ios.tag_prefix", "ios_v", "ios.tag_prefix"),
        ("ios.bundle_id", "nodots", "ios.bundle_id"),
        ("ios.asc_app_id", "12345", "ios.asc_app_id"),
        ("ios.team_id", "23rv7fyh36", "ios.team_id"),
        ("ios.project", "ios/calorietracker", "ios.project"),
        ("ios.scheme", 'bad"scheme', "ios.scheme"),
        ("ios.app_target", "", "ios.app_target"),
        ("ios.info_plist", "ios\\Info.plist", "ios.info_plist"),
        ("ios.info_plist", "ios/\nInfo.plist", "ios.info_plist"),
        ("ios.locale", "english", "ios.locale"),
        ("ios.gate_jobs", [], "ios.gate_jobs"),
        ("ios.gate_jobs[0].id", "Bad", "ios.gate_jobs[0].id"),
        ("ios.gate_jobs[0].id", "build", "reserved"),
        ("ios.gate_jobs[0].id", "web", "reserved"),
        ("ios.gate_jobs[1].id", "ios-tests", "listed twice"),
        ("ios.gate_jobs[0].uses", "apoorvdarshan/x/.github/workflows/y.yml@main", "ios.gate_jobs[0].uses"),
        ("ios.build.developer_dir", "Applications/Xcode.app", "ios.build.developer_dir"),
        ("ios.build.developer_dir", "/Applications/Xcode-Beta.app/Contents/Developer", "released Xcode"),
        ("ios.build.checkout", [], "ios.build.checkout"),
        ("ios.build.checkout", ["ios/"], "ios.build.checkout[0]"),
        ("ios.build.checkout", ["/ios/../secrets/"], "ios.build.checkout[0]"),
        ("ios.build.checkout", ["/ios/ "], "ios.build.checkout[0]"),
        ("ios.build.package_cache_files", ["/abs/Package.resolved"], "ios.build.package_cache_files[0]"),
        ("ios.build.pre_archive", ["scripts/ci/hook.rb"], "ios.build.pre_archive[0]"),
        ("ios.build.runner", "macos 26", "ios.build.runner"),
        ("ios.build.runner", "", "ios.build.runner"),
        ("ios.build.runner", 26, "ios.build.runner"),
        ("ios.listing.source", "files", "ios.listing.source"),
        ("ios.whats_new.source", "release-notes", "ios.whats_new.source"),
        ("ios.whats_new.heading", "", "ios.whats_new.heading"),
        ("ios.screenshots.sets[0].display_type", "APP_IPHONE_65", "ios.screenshots.sets[0].display_type"),
        ("ios.screenshots.sets[0].required", False, "required = true"),
        ("ios.screenshots.sets[0].required", "true", "ios.screenshots.sets[0].required"),
        ("ios.screenshots.sets[3].required", True, "WATCH set must have required = false"),
        ("ios.screenshots.sets[1].display_type", "APP_IPHONE_67", "listed twice"),
        ("ios.creative.dir", "", "ios.creative.dir"),
        ("ios.export_compliance", "no", "ios.export_compliance"),
        ("ios.export_compliance", False, "ios.export_compliance"),
        ("ios.creative.sizes", {}, "ios.creative.sizes"),
        ("ios.creative.sizes", [["header/a.png", 1]], "ios.creative.sizes"),
        ("ios.creative.sizes", {"header/a.png": 0}, "positive integer"),
        ("ios.creative.sizes", {"header/a.png": "17"}, "positive integer"),
        ("ios.creative.sizes", {"header/a.png": True}, "positive integer"),
        ("ios.creative.sizes", {"logo/a.png": 1}, "the slot header or search-results"),
        ("ios.creative.sizes", {"header/sub/a.png": 1}, "the slot header or search-results"),
        ("ios.creative.sizes", {"header/../a.png": 1}, "'..' segment"),
        ("ios.creative.sizes", {"header/a.jpg": 1}, "the header slot takes .png files"),
        ("ios.creative.sizes", {"search-results/a.gif": 1}, "takes .png, .jpg, .jpeg files"),
        ("ios.creative.sizes", {"header/a.png": 1, "header/b.png": 2}, "the header slot takes one image"),
        ("ios.products.iap_route", "review-item", "ios.products.iap_route"),
        ("ios.github_release.title", "iOS {commit}", "unknown placeholder"),
        ("ios.github_release.title", "iOS {version!r}", "unknown placeholder"),
        ("ios.github_release.title", "iOS {version", "not a valid template"),
        ("android.tag_prefix", "Android-v", "android.tag_prefix"),
        ("android.package", "calorietracker", "android.package"),
        ("android.gradle_file", "android/../app.kts", "android.gradle_file"),
        ("android.upload_cert_sha256", "35D739F4" + "0" * 56, "android.upload_cert_sha256"),
        ("android.locale", "e", "android.locale"),
        ("android.build.tasks", [], "android.build.tasks"),
        ("android.build.tasks", ["bundle release"], "android.build.tasks[0]"),
        ("android.build.signing", "gradle-properties", "android.build.signing"),
        ("android.build.checkout", ["android/"], "android.build.checkout[0]"),
        ("android.listing.source", "files", "android.listing.source"),
        ("android.screenshots.pad_to_9_16", False, "only true"),
        ("android.screenshots.pad_to_9_16", "true", "android.screenshots.pad_to_9_16"),
        ("android.github_release.asset_name", "{slug}/{version}", "safe file name"),
        ("android.github_release.asset_name", "{name}-v{version}", "unknown placeholder"),
    ]

    def test_every_key_rejects_a_bad_value(self) -> None:
        for dotted, value, fragment in self.BAD:
            with self.subTest(key=dotted, value=value), self.assertRaises(config.ConfigError) as caught:
                config.parse(mutated(dotted, value))
            self.assertIn(fragment, str(caught.exception))

    def test_unknown_keys_fail_at_every_level(self) -> None:
        tables = ["", "app", "release_notes", "catalog", "checks", "ios", "ios.build", "ios.listing",
                  "ios.whats_new", "ios.screenshots", "ios.screenshots.sets[0]", "ios.creative", "ios.products",
                  "ios.github_release", "ios.gate_jobs[0]", "android", "android.build", "android.listing",
                  "android.whats_new", "android.screenshots", "android.github_release", "android.gate_jobs[0]"]
        for table in tables:
            data = fud_ai_data()
            node = data
            if table:
                parent, key = walk(data, table)
                node = parent[key]
            node["bogus"] = 1
            with self.subTest(table=table), self.assertRaisesRegex(config.ConfigError, "bogus: unknown key"):
                config.parse(data)

    def test_the_standard_flow_and_its_old_name(self) -> None:
        cfg = config.parse(fud_ai_data())
        self.assertEqual((cfg.ios.flow, cfg.android.flow, cfg.ported_flow), ("standard", "standard", False))
        for value in ("standard", "fud-ai"):
            data = fud_ai_data()
            data["ios"]["flow"] = value
            data["android"]["flow"] = value
            cfg = config.parse(data)
            with self.subTest(flow=value):
                self.assertEqual((cfg.ios.flow, cfg.android.flow, cfg.ported_flow), ("standard", "standard", False))
        data = fud_ai_data()
        data["ios"]["flow"] = "fud"
        with self.assertRaisesRegex(config.ConfigError, r"ios.flow: 'fud' is not supported .*accepted: standard, "
                                                        r"scowld, .*also fud-ai \(= standard\)"):
            config.parse(data)
        data = fud_ai_data()
        data["android"]["flow"] = "scowld"
        with self.assertRaisesRegex(config.ConfigError, "android.flow: 'scowld'"):
            config.parse(data)

    def test_the_build_runner(self) -> None:
        self.assertIsNone(config.parse(fud_ai_data()).ios.build.runner)
        cfg = config.parse(mutated("ios.build.developer_dir", "/Applications/Xcode_26.app/Contents/Developer"))
        self.assertIsNone(cfg.ios.build.runner)
        data = fud_ai_data()
        data["ios"]["build"]["runner"] = "macos-26"
        self.assertEqual(config.parse(data).ios.build.runner, "macos-26")

    def test_the_watch_set_comes_last(self) -> None:
        data = fud_ai_data()
        sets = data["ios"]["screenshots"]["sets"]
        sets.insert(0, sets.pop())
        with self.assertRaisesRegex(config.ConfigError, "WATCH set must come last"):
            config.parse(data)
        data = fud_ai_data()
        data["ios"]["screenshots"]["sets"] = [data["ios"]["screenshots"]["sets"][-1]]
        with self.assertRaisesRegex(config.ConfigError, "iPhone or iPad set"):
            config.parse(data)

    def test_templates_render(self) -> None:
        self.assertEqual(config.render_template("iOS {version} (build {build})", version="7.2", build="40"),
                         "iOS 7.2 (build 40)")
        self.assertEqual(config.check_template("fudai-v{version}", "k", config.ASSET_NAME_PLACEHOLDERS),
                         "fudai-v{version}")
        with self.assertRaisesRegex(config.ConfigError, "unknown placeholder"):
            config.check_template("{}", "k", ("version",))
        with self.assertRaisesRegex(config.ConfigError, "unknown placeholder"):
            config.check_template("{version:>5}", "k", ("version",))


class PathSafetyTests(unittest.TestCase):
    def test_paths(self) -> None:
        for bad in ("/etc/passwd", "a/../b", "a//b", "./a", "a/", "a\\b", "a\x00b", "a\nb", ""):
            with self.subTest(path=bad), self.assertRaises(config.ConfigError):
                config.check_path(bad, "k")
        self.assertEqual(config.check_path("store/catalog/products.json", "k"), "store/catalog/products.json")

    def test_sparse_patterns(self) -> None:
        for good in ("/ios/", "/shared/workout-vectors/sample-pack.txt", "!/shared/workout-vectors/*_v2_*.png"):
            self.assertEqual(config.check_sparse_pattern(good, "k"), good)
        for bad in ("ios/", "/ios/ ", "!ios", "/a/../b", "/a\rb", "", 5):
            with self.subTest(pattern=bad), self.assertRaises(config.ConfigError):
                config.check_sparse_pattern(bad, "k")

    def test_a_symlink_out_of_the_app_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "store").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(config.ConfigError, "outside the app repository"):
                config.resolve_inside(root, "store/catalog/products.json", "catalog.path")
            self.assertEqual(config.resolve_inside(root, "ios/x", "k"), (root / "ios/x").resolve())


def scaffold_config(*, info_plist: str | None) -> str:
    """A config for trees.write_new_app_tree (ios only)."""
    plist = f'info_plist = "{info_plist}"\n' if info_plist else ""
    return ('schema = 1\n[app]\nname = "My App"\nslug = "my-app"\n[ios]\ntag_prefix = "v"\n'
            'bundle_id = "com.example.myapp"\nasc_app_id = "1234567890"\nteam_id = "ABCDE12345"\n'
            'project = "ios/MyApp.xcodeproj"\nscheme = "MyApp"\napp_target = "MyApp"\n' + plist +
            'gate_jobs = [{ id = "ios-tests", uses = "./.github/workflows/ios-tests.yml" }]\n'
            '[ios.build]\ndeveloper_dir = "/Applications/Xcode_27.app/Contents/Developer"\ncheckout = ["/ios/"]\n')


class StepTableTests(unittest.TestCase):
    """listing, whats-new and screenshots with --platform all do only the platforms whose table the config has."""

    def test_platform_all_skips_a_missing_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = trees.write_new_app_tree(Path(tmp) / "app")
            trees.write_text(root, "store/store-release.toml", scaffold_config(info_plist="ios/MyApp/Info.plist"))
            out = Path(tmp) / "out"
            for command, verb, extra, table in (
                    ("listing", "prepare", [], "[ios.listing] or [android.listing]"),
                    ("whats-new", "prepare", ["--tag", "dry-run"], "[ios.whats_new] or [android.whats_new]"),
                    ("screenshots", "stage", [], "[[ios.screenshots.sets]] or [android.screenshots]")):
                with self.subTest(command=command):
                    code, stdout, stderr = run_cli(["--app-root", str(root), command, verb, "--platform", "all",
                                                    "--out", str(out), *extra])
                    self.assertEqual(code, 0, stderr)
                    self.assertIn(f"the config has no {table}; nothing to do", stdout)
                    # An explicit platform is a request for that table's files: it still fails without them.
                    try:
                        code, _, stderr = run_cli(["--app-root", str(root), command, verb, "--platform", "ios",
                                                   "--out", str(out), *extra])
                    except FileNotFoundError as exc:
                        code, stderr = 1, str(exc)
                    self.assertEqual(code, 1, stderr)
            self.assertFalse(out.exists())

    def test_platform_all_keeps_every_platform_with_its_table(self) -> None:
        app = support_apps.default_app()
        cli_app = mock.Mock(config=app.config)
        for command in ("listing", "whats-new", "screenshots"):
            self.assertEqual(cli.narrow_to_tables("all", cli_app, command), "all")
        data = fud_ai_data()
        del data["ios"]["whats_new"]
        cli_app = mock.Mock(config=config.parse(data))
        self.assertEqual(cli.narrow_to_tables("all", cli_app, "whats-new"), "play")
        self.assertEqual(cli.narrow_to_tables("all", cli_app, "listing"), "all")
        self.assertEqual(cli.narrow_to_tables("ios", cli_app, "whats-new"), None)


class ConfigFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = support_apps.fixture_app("fud-ai", Path(tmp.name) / "app", screenshots=True)

    def test_every_named_file_exists(self) -> None:
        checked = config.check_files(self.app.config, self.app.root)
        self.assertIn("scripts/ci/check_catalog.py", checked)
        self.assertIn("ios/calorietracker/Info.plist (INFOPLIST_FILE)", checked)

    def test_a_missing_hook_or_folder_fails(self) -> None:
        (self.app.root / "scripts/ci/check_catalog.py").unlink()
        with self.assertRaisesRegex(config.ConfigError, r"catalog.checks\[0\]"):
            config.check_files(self.app.config, self.app.root)

    def test_without_info_plist_xcode_must_generate_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = trees.write_new_app_tree(Path(tmp) / "generated", generated_plist=True)
            text = scaffold_config(info_plist=None)
            cfg = config.parse(tomllib.loads(text))
            self.assertIsNone(cfg.ios.info_plist)
            self.assertIn("MyApp: no INFOPLIST_FILE (Xcode generates the Info.plist)", config.check_files(cfg, root))
            self.assertEqual(ios_release.check_export_compliance(root / cfg.ios.pbxproj, "MyApp"), "NO")
            # An INFOPLIST_FILE the config does not name would not be checked out: name it.
            root = trees.write_new_app_tree(Path(tmp) / "with-file")
            with self.assertRaisesRegex(config.ConfigError, r'MyApp/Info.plist \(ios/MyApp/Info.plist\); set '
                                                            r'ios.info_plist = "ios/MyApp/Info.plist"'):
                config.check_files(cfg, root)
            (root / "ios/MyApp.xcodeproj/project.pbxproj").write_text(trees.pbxproj(
                {"MyApp": {}}, project_name="MyApp", marketing="1.0", build="1"))
            with self.assertRaisesRegex(config.ConfigError, "neither an INFOPLIST_FILE nor GENERATE_INFOPLIST_FILE"):
                config.check_files(cfg, root)

    def test_info_plist_must_be_the_app_targets_infoplist_file(self) -> None:
        data = fud_ai_data()
        data["ios"]["info_plist"] = "ios/calorietracker/Other.plist"
        (self.app.root / "ios/calorietracker/Other.plist").write_bytes(trees.info_plist("x"))
        with self.assertRaisesRegex(config.ConfigError, "INFOPLIST_FILE"):
            config.check_files(config.parse(data), self.app.root)

    def test_the_watch_folder_is_optional(self) -> None:
        import shutil

        shutil.rmtree(self.app.root / "store/screenshots/watch")
        config.check_files(self.app.config, self.app.root)
        shutil.rmtree(self.app.root / "store/screenshots/ipad")
        with self.assertRaisesRegex(config.ConfigError, r"ios.screenshots.sets\[1\].dir"):
            config.check_files(self.app.config, self.app.root)


class ConfigPinTests(unittest.TestCase):
    """config check --pinned / --files: the facts fud-ai's unit tests pinned in its real files."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = support_apps.fixture_app("fud-ai", Path(tmp.name) / "app", screenshots=True)
        self.creative = self.app.root / "store/creative/ios"

    def pinned(self, cfg: config.Config | None = None) -> list[str]:
        return config.check_pinned(cfg or self.app.config, self.app.root)

    def fails(self, fragment: str, cfg: config.Config | None = None) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            self.pinned(cfg)
        self.assertIn(fragment, str(caught.exception))

    def test_fud_ais_pins_hold_in_the_synthetic_tree(self) -> None:
        self.assertEqual(self.app.config.ios.export_compliance, "NO")
        self.assertEqual(self.app.config.ios.creative_sizes, (("header/fud-ai-header-orb-horizon.png", 1763332),
                                                              ("search-results/fud-ai-search-voice.png", 2670930)))
        self.assertEqual(self.pinned(), ["ios.export_compliance = NO in every calorietracker configuration",
                                         "ios.creative.sizes: 2 image(s) in store/creative/ios as pinned"])

    def test_the_slot_table_is_the_store_steps(self) -> None:
        from store_release.ios import asc

        self.assertEqual(list(config.CREATIVE_SLOT_SUFFIXES), [slot[0] for slot in asc.CREATIVE_SLOTS])
        for folder, _, _, formats in asc.CREATIVE_SLOTS:
            with self.subTest(slot=folder):
                suffixes = config.CREATIVE_SLOT_SUFFIXES[folder]
                self.assertEqual({asc.CREATIVE_EXTENSIONS[suffix] for suffix in suffixes}, set(formats))
                self.assertEqual({s for s, kind in asc.CREATIVE_EXTENSIONS.items() if kind in formats}, set(suffixes))

    def test_nothing_pinned_checks_nothing(self) -> None:
        data = fud_ai_data()
        del data["ios"]["export_compliance"], data["ios"]["creative"]["sizes"]
        self.assertEqual(self.pinned(config.parse(data)), [])
        example = config.load(EXAMPLE_TOML)
        self.assertEqual(config.check_pinned(example, self.app.root), [])

    def test_another_export_compliance_answer_fails(self) -> None:
        (self.app.root / "ios/calorietracker/Info.plist").write_bytes(plistlib.dumps(
            {"ITSAppUsesNonExemptEncryption": True, "ITSEncryptionExportComplianceCode": "code-1"}))
        # A valid answer (YES with its code), but not the pinned one.
        self.fails("the calorietracker target declares ITSAppUsesNonExemptEncryption = YES, but the config pins NO")
        data = fud_ai_data()
        data["ios"]["export_compliance"] = "YES"
        self.assertEqual(self.pinned(config.parse(data))[0], "ios.export_compliance = YES in every calorietracker "
                                                             "configuration")
        (self.app.root / "ios/calorietracker/Info.plist").write_bytes(plistlib.dumps({}))
        self.fails("ios.export_compliance: the calorietracker target's Debug configuration does not declare")

    def test_a_changed_renamed_extra_or_invalid_image_fails(self) -> None:
        header = self.creative / "header/fud-ai-header-orb-horizon.png"
        search = self.creative / "search-results/fud-ai-search-voice.png"
        original = header.read_bytes()
        header.write_bytes(original[:-12] + pngs.chunk(b"prVt", b"x") + original[-12:])  # re-encoded: 13 bytes more
        self.fails("header/fud-ai-header-orb-horizon.png is 1763345 bytes, pinned at 1763332")
        header.write_bytes(original)
        header.rename(header.with_name("renamed.png"))
        self.fails("header/fud-ai-header-orb-horizon.png is missing (pinned at 1763332 bytes)")
        self.fails("header/renamed.png is not pinned")
        header.with_name("renamed.png").rename(header)
        self.pinned()
        (search.parent / "second.jpg").write_bytes(pngs.jpeg(1920, 1280))
        self.fails("search-results/second.jpg is not pinned")
        (search.parent / "second.jpg").unlink()
        (search.parent / "notes.md").write_text("not an image")  # other files never count
        self.pinned()
        search.write_bytes(pngs.rgba(3840, 2560))
        data = fud_ai_data()
        data["ios"]["creative"]["sizes"]["search-results/fud-ai-search-voice.png"] = search.stat().st_size
        self.fails("search-results/fud-ai-search-voice.png stores an alpha channel", config.parse(data))

    def test_the_cli(self) -> None:
        code, out, err = run_cli(["--app-root", str(self.app.root), "config", "check", "--pinned"])
        self.assertEqual(code, 0, err)
        self.assertIn("pinned: ios.creative.sizes: 2 image(s)", out)
        code, out, err = run_cli(["--app-root", str(self.app.root), "config", "check", "--files"])
        self.assertEqual(code, 0, err)
        self.assertIn("pinned: ios.export_compliance = NO", out)
        (self.creative / "header/fud-ai-header-orb-horizon.png").write_bytes(pngs.rgb(3840, 1646))
        for flag in ("--pinned", "--files"):
            code, _, err = run_cli(["--app-root", str(self.app.root), "config", "check", flag])
            with self.subTest(flag=flag):
                self.assertEqual(code, 1)
                self.assertIn("error: ios.creative.sizes: store/creative/ios differs from the pinned images", err)
        code, out, err = run_cli(["--app-root", str(self.app.root), "config", "check"])
        self.assertEqual(code, 0, "a plain config check reads no app file")


FUD_AI_PATHS = {
    "ios-prepare": ["/store/store-release.toml", "/.github/workflows/ios-release.yml", "/store/",
                    "/ios/calorietracker.xcodeproj/project.pbxproj", "/ios/calorietracker/Info.plist",
                    "/RELEASE_NOTES.md", "/APPSTORE.md", "/scripts/ci/check_catalog.py"],
    "ios-build": ["/store/store-release.toml", "/ios/calorietracker.xcodeproj/project.pbxproj",
                  "/scripts/ci/ensure_metal_toolchain.sh", "/ios/", "/local-models/legal/",
                  "/shared/workout-vectors/sample-pack.txt", "/shared/workout-vectors/exercise-visual-manifest.json"],
    "ios-wait-for-build": ["/store/store-release.toml"],
    "ios-app-store-connect": ["/store/store-release.toml", "/store/", "/APPSTORE.md"],
    "ios-source-archive": ["/store/store-release.toml"],
    "ios-github-release": ["/store/store-release.toml", "/RELEASE_NOTES.md",
                           "/ios/calorietracker.xcodeproj/project.pbxproj"],
    "ios-store-plan": ["/store/store-release.toml", "/store/", "/APPSTORE.md",
                       "/ios/calorietracker.xcodeproj/project.pbxproj"],
    "android-gates": ["/store/store-release.toml", "/.github/workflows/android-release.yml",
                      "/android/app/build.gradle.kts", "/RELEASE_NOTES.md", "/PLAYSTORE.md"],
    "android-publish": ["/store/store-release.toml", "/store/", "/RELEASE_NOTES.md", "/PLAYSTORE.md",
                        "/scripts/ci/check_catalog.py", "/android/", "/ios/calorietracker/Resources/FreeExerciseDB/dist/",
                        "/local-models/legal/", "/shared/workout-vectors/", "!/shared/workout-vectors/*_v2_*.png"],
    "android-store-plan": ["/store/store-release.toml", "/android/app/build.gradle.kts", "/store/", "/PLAYSTORE.md",
                           "/RELEASE_NOTES.md"],
    "checks": ["/store/store-release.toml", "/.github/workflows/", "/store/", "/APPSTORE.md", "/PLAYSTORE.md",
               "/RELEASE_NOTES.md", "/ios/calorietracker.xcodeproj/project.pbxproj", "/ios/calorietracker/Info.plist",
               "/android/app/build.gradle.kts", "/scripts/ci/check_catalog.py", "/scripts/ci/ensure_metal_toolchain.sh"],
}


class PathsTests(unittest.TestCase):
    def env(self, job: str) -> dict[str, str]:
        workflow = "android-release.yml" if job.startswith("android") else "ios-release.yml"
        return {"GITHUB_REPOSITORY": "apoorvdarshan/fud-ai",
                "GITHUB_WORKFLOW_REF": f"apoorvdarshan/fud-ai/.github/workflows/{workflow}@refs/tags/v"}

    def test_fud_ais_exact_lists(self) -> None:
        cfg = config.load(FUD_AI_TOML)
        for job, expected in FUD_AI_PATHS.items():
            with self.subTest(job=job):
                self.assertEqual(paths.patterns(cfg, job, config_path="store/store-release.toml",
                                                environ=self.env(job)), expected)
        self.assertEqual(sorted(FUD_AI_PATHS), sorted(paths.JOBS))

    def test_config_patterns_come_last_and_duplicates_go(self) -> None:
        data = fud_ai_data()
        data["ios"]["build"]["checkout"] = ["/scripts/ci/ensure_metal_toolchain.sh", "/ios/", "!/ios/big/"]
        lines = paths.patterns(config.parse(data), "ios-build", config_path="store/store-release.toml", environ={})
        self.assertEqual(lines, ["/store/store-release.toml", "/ios/calorietracker.xcodeproj/project.pbxproj",
                                 "/scripts/ci/ensure_metal_toolchain.sh", "/ios/", "!/ios/big/"])
        self.assertEqual(lines[-1], "!/ios/big/")

    def test_paths_outside_store_are_added(self) -> None:
        data = fud_ai_data()
        data["catalog"]["path"] = "billing/products.json"
        data["ios"]["creative"]["dir"] = "art/creative"
        lines = paths.patterns(config.parse(data), "ios-app-store-connect", config_path="store/store-release.toml",
                               environ={})
        self.assertIn("/billing/products.json", lines)
        self.assertIn("/art/creative/", lines)

    def test_the_caller_must_be_this_repositorys_workflow(self) -> None:
        cfg = config.load(FUD_AI_TOML)
        for ref, repo in (("someone/else/.github/workflows/ios-release.yml@x", "apoorvdarshan/fud-ai"),
                          ("apoorvdarshan/fud-ai/scripts/x.yml@x", "apoorvdarshan/fud-ai"),
                          ("", "")):
            with self.subTest(ref=ref), self.assertRaises(config.ConfigError):
                paths.patterns(cfg, "ios-prepare", config_path="store/store-release.toml",
                               environ={"GITHUB_WORKFLOW_REF": ref, "GITHUB_REPOSITORY": repo})
        with self.assertRaisesRegex(config.ConfigError, "unknown job"):
            paths.patterns(cfg, "deploy", config_path="store/store-release.toml", environ={})

    def test_a_platform_job_needs_its_table(self) -> None:
        data = fud_ai_data()
        del data["android"]
        with self.assertRaisesRegex(config.ConfigError, r"\[android\]"):
            paths.patterns(config.parse(data), "android-gates", config_path="store/store-release.toml", environ={})


class SwitchTableTests(unittest.TestCase):
    """A switch this run uses fails `gates` when its config table is missing."""

    IOS = {"listing": "IOS_UPLOAD_LISTING", "whats_new": "IOS_UPLOAD_WHATS_NEW",
           "screenshots": "IOS_UPLOAD_SCREENSHOTS", "creative": "IOS_UPLOAD_CREATIVE_ASSETS",
           "products": "IOS_SUBMIT_REVIEW"}
    ANDROID = {"listing": "ANDROID_UPLOAD_LISTING", "whats_new": "ANDROID_UPLOAD_WHATS_NEW",
               "screenshots": "ANDROID_UPLOAD_SCREENSHOTS"}

    def app_without(self, platform: str, table: str) -> support_apps.AppContext:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = fud_ai_data()
        del data[platform][table]
        app = support_apps.fixture_app("fud-ai", Path(tmp.name) / "app")
        app.config = config.parse(data)
        return app

    def test_ios_gates(self) -> None:
        for table, switch in self.IOS.items():
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "ios-v7.2", "IOS_RELEASE_AUTOMATION": "true",
                   switch: "true"}
            if switch == "IOS_SUBMIT_REVIEW":
                env["IOS_UPLOAD_BUILD"] = "true"
            app = self.app_without("ios", table)
            with self.subTest(table=table), support_apps.configured(app), \
                    mock.patch.object(ios_release, "check_export_compliance", return_value="NO"), \
                    mock.patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                ios_release.main(["gates"])
            self.assertIn(f"{switch} is on, but store/store-release.toml has no [ios.{table}]", err.getvalue())
            # Off (or ignored with the master off), the missing table does not matter.
            with support_apps.configured(app), mock.patch.dict(os.environ, {**env, "IOS_RELEASE_AUTOMATION": "false"},
                                                               clear=True), redirect_stdout(io.StringIO()):
                ios_release.main(["gates"])

    def test_android_gates(self) -> None:
        for table, switch in self.ANDROID.items():
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "android-v7.2",
                   "ANDROID_RELEASE_AUTOMATION": "true", "ANDROID_INTERNAL_TESTING": "true", switch: "true"}
            app = self.app_without("android", table)
            with self.subTest(table=table), support_apps.configured(app), \
                    mock.patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                android_release.main(["gates"])
            self.assertIn(f"{switch} is on, but store/store-release.toml has no [android.{table}]", err.getvalue())


class RevenueCatGateTests(unittest.TestCase):
    def gate(self, value: str | None, fn=None) -> tuple[int, str, str]:
        env = {k: v for k, v in os.environ.items() if k != "STORE_SYNC_REVENUECAT"}
        if value is not None:
            env["STORE_SYNC_REVENUECAT"] = value
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            try:
                (fn or catalog.revenuecat_gate)()
            except SystemExit as exc:
                return exc.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_the_switch_alone_is_read_before_anything_is_built(self) -> None:
        # Android gates: an invalid value fails there, not after the Play listing changed in the release job.
        code, _, err = self.gate("bogus", catalog.check_switch)
        self.assertEqual(code, 1)
        self.assertIn("STORE_SYNC_REVENUECAT='bogus' is not a valid switch value", err)
        for value in (None, "", "false", "0"):
            code, out, _ = self.gate(value, catalog.check_switch)
            with self.subTest(value=value):
                self.assertEqual(code, 0)
                self.assertIn("STORE_SYNC_REVENUECAT is off", out)
                self.assertNotIn("catalog:", out, "the catalog is the gate's job, later")
        # On is valid here; the gate in the release job still fails closed where it always did.
        code, out, _ = self.gate("true", catalog.check_switch)
        self.assertEqual(code, 0)
        self.assertIn("::warning title=RevenueCat::STORE_SYNC_REVENUECAT is on", out)
        app = support_apps.default_app()
        code, out, err = run_cli(["--app-root", str(app.root), "revenuecat-gate", "--check-switch"],
                                 env={"STORE_SYNC_REVENUECAT": "maybe"})
        self.assertEqual(code, 1)
        self.assertIn("is not a valid switch value", err)
        code, out, err = run_cli(["--app-root", str(app.root), "revenuecat-gate", "--check-switch"])
        self.assertEqual(code, 0, err)

    def test_counts_and_stays_dry_unless_on(self) -> None:
        for value in (None, "", "false", "off", "0"):
            code, out, _ = self.gate(value)
            with self.subTest(value=value):
                self.assertEqual(code, 0)
                self.assertIn("catalog: 11 products, 2 entitlements, 2 offerings", out)
                self.assertIn("dry-run only", out)

    def test_on_fails_closed_and_invalid_values_fail(self) -> None:
        code, _, err = self.gate("true")
        self.assertEqual(code, 1)
        self.assertIn("not implemented yet", err)
        self.assertIn("(store/catalog/products.json, store/catalog/revenuecat.json)", err)
        code, _, err = self.gate("maybe")  # D14: today's loose read would treat this as off
        self.assertEqual(code, 1)
        self.assertIn("STORE_SYNC_REVENUECAT='maybe' is not a valid switch value", err)

    def test_switch_parsing_is_shared(self) -> None:
        self.assertTrue(switches.parse_switch("X", " YES "))
        self.assertFalse(switches.parse_switch("X", None))
        with self.assertRaises(switches.SwitchError):
            switches.parse_switch("X", "2")


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = support_apps.fixture_app("fud-ai", Path(tmp.name) / "app")
        self.products = json.loads((self.app.root / "store/catalog/products.json").read_text())

    def check(self, products: dict | None = None, *, hooks_on: bool = False) -> tuple[int, str, str]:
        if products is not None:
            (self.app.root / "store/catalog/products.json").write_text(json.dumps(products))
        out, err = io.StringIO(), io.StringIO()
        with support_apps.configured(self.app):
            if not hooks_on:
                catalog.CHECKS = ()
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    catalog.check()
                except SystemExit as exc:
                    return exc.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_the_fud_ai_catalog_passes_with_the_hook(self) -> None:
        code, out, err = self.check(hooks_on=True)
        self.assertEqual(code, 0, err)
        self.assertIn("ok: 11 products (11 on Play)", out)
        # The hook (fud-ai's validate_catalog.py) prints to the process's own stdout.
        self.assertIn("hook: scripts/ci/check_catalog.py", out)

    def test_generic_rules(self) -> None:
        cases = []
        missing = copy.deepcopy(self.products)
        del missing["tips"]
        cases.append((missing, "missing products.tips"))
        not_list = copy.deepcopy(self.products)
        not_list["tips"] = {"a": 1}
        cases.append((not_list, "missing products.tips"))
        bad_id = copy.deepcopy(self.products)
        bad_id["tips"][0]["id"] = ""
        cases.append((bad_id, "invalid id in tips"))
        prefix = copy.deepcopy(self.products)
        prefix["tips"][0]["id"] = "com.other.tip"
        cases.append((prefix, "unexpected product id prefix: com.other.tip"))
        duplicate = copy.deepcopy(self.products)
        duplicate["tips"][1]["id"] = duplicate["tips"][0]["id"]
        cases.append((duplicate, "duplicate product ids in products.json"))
        play_dup = copy.deepcopy(self.products)
        play_dup["tips"][1]["play"]["product_id"] = play_dup["tips"][0]["play"]["product_id"]
        cases.append((play_dup, "duplicate Play product ids in products.json"))
        no_play = copy.deepcopy(self.products)
        del no_play["tips"][0]["play"]
        cases.append((no_play, "lists android but has no play block"))
        rc = copy.deepcopy(self.products)
        rc["subscriptions"][0]["play"]["revenuecat_product_id"] = "plus-monthly"
        cases.append((rc, "revenuecat_product_id must be 'plus:monthly'"))
        for products, message in cases:
            with self.subTest(message=message):
                code, _, err = self.check(products)
                self.assertEqual(code, 1)
                self.assertIn(message, err)

    def test_without_play_rules_play_blocks_are_not_checked(self) -> None:
        no_play = copy.deepcopy(self.products)
        del no_play["tips"][0]["play"]
        (self.app.root / "store/catalog/products.json").write_text(json.dumps(no_play))
        with support_apps.configured(self.app):
            catalog.CHECKS = ()
            catalog.PLAY_RULES = False
            out = io.StringIO()
            with redirect_stdout(out):
                catalog.check()
        self.assertIn("ok: 11 products\n", out.getvalue())

    def test_a_failing_hook_stops_the_check(self) -> None:
        (self.app.root / "scripts/ci/check_catalog.py").write_text("import sys\nsys.exit(3)\n")
        with self.assertRaisesRegex(hooks.HookError, "exited 3"):
            self.check(hooks_on=True)

    def test_no_catalog_is_nothing_to_check(self) -> None:
        with support_apps.configured(self.app):
            catalog.PRODUCTS = None
            out = io.StringIO()
            with redirect_stdout(out):
                catalog.check()
        self.assertIn("nothing to check", out.getvalue())


class HookTests(unittest.TestCase):
    SECRET_LIKE = ["MY_TOKEN", "SOME_SECRET", "DB_PASSWORD", "GPG_PASSPHRASE", "X_CREDENTIAL", "API_KEY", "KEY_ID",
                   "IOS_DEV_SIGNING_P12_BASE64", "APP_STORE_CONNECT_API_KEY_P8", "ASC_KEY_ID", "ASC_ISSUER_ID",
                   "APP_STORE_CONNECT_ISSUER_ID", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS",
                   "PLAY_SERVICE_ACCOUNT_JSON", "GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
                   "ACTIONS_RUNTIME_URL", "github_token", "Signing_P8_path"]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "hooks").mkdir()
        (self.root / "hooks/env.sh").write_text('#!/bin/bash\nenv > "$PWD/env-sh.txt"\n')
        (self.root / "hooks/env.py").write_text(
            "import json, os\nopen(os.path.join(os.getcwd(), 'env-py.json'), 'w').write(json.dumps(dict(os.environ)))\n")
        (self.root / "hooks/fail.sh").write_text("exit 7\n")

    def test_scrubbed_environment_cwd_and_dispatch(self) -> None:
        environ = {"PATH": os.environ.get("PATH", ""), "HOME": "/home/x", "SAFE_VAR": "kept"}
        environ.update({name: "do-not-leak" for name in self.SECRET_LIKE})
        with redirect_stdout(io.StringIO()):
            hooks.run(self.root, ["hooks/env.sh", "hooks/env.py"], env=environ,
                      env_extra={"MARKETING_VERSION": "7.2", "BUILD_NUMBER": "40"})
        shell = (self.root / "env-sh.txt").read_text()
        py = json.loads((self.root / "env-py.json").read_text())
        for name in self.SECRET_LIKE:
            with self.subTest(name=name):
                self.assertNotIn(name, py)
                self.assertNotIn(f"{name}=", shell)
        self.assertNotIn("do-not-leak", shell)
        self.assertEqual((py["SAFE_VAR"], py["MARKETING_VERSION"], py["BUILD_NUMBER"]), ("kept", "7.2", "40"))

    def test_a_non_zero_exit_and_unsafe_paths_fail(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(hooks.HookError, "hook hooks/fail.sh exited 7"):
            hooks.run(self.root, ["hooks/fail.sh"], env={"PATH": os.environ.get("PATH", "")})
        for bad in ("../x.sh", "hooks/missing.sh", "hooks/x.rb", "/abs/x.sh"):
            with self.subTest(bad=bad), redirect_stdout(io.StringIO()), self.assertRaises(hooks.HookError):
                hooks.run(self.root, [bad], env={})
        self.assertEqual(hooks.command("a/b.py"), ["python3", "-u", "a/b.py"])
        self.assertEqual(hooks.command("a/b.sh"), ["bash", "a/b.sh"])


def stage_args(**values) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    stage.add_stage_arguments(parser)
    argv = []
    for key, value in values.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return parser.parse_args(argv)


IOS_PUSH = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "ios-v7.2"}
IOS_DISPATCH = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main"}
ANDROID_PUSH = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "android-v7.2"}
ASC_GOOD = dict(stage="ios-app-store-connect", gates="ios-tests=success quality=success", marketing_version="7.2",
                build_number="40", build_id="0f6c8e5a-1b2c-4d3e-8f90-123456789abc", upload_build="true",
                listing="false", whats_new="false", screenshots="false", creative="false", submit="true",
                public_release="false")
PUBLISH_RELEASE = dict(stage="android-publish", gates="quality=success", mode="release", build="true",
                       github_release="true", listing="false", whats_new="true", screenshots="false",
                       production="false", play_upload="true", play_tracks="internal,alpha")
PUBLISH_VALIDATE = dict(stage="android-publish", gates="quality=skipped", mode="validate", build="true",
                        github_release="false", listing="false", whats_new="false", screenshots="false",
                        production="false", play_upload="false", play_tracks="")


class StageCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = support_apps.default_app()
        self.cfg = self.app.config

    def check(self, env: dict, **values) -> None:
        quiet(stage.stage_check, values["stage"], stage_args(**values), self.cfg, environ=env)

    def fails(self, env: dict, fragment: str, **values) -> None:
        with self.assertRaises(stage.StageError) as caught:
            self.check(env, **values)
        self.assertIn(fragment, str(caught.exception))

    def test_good_inputs_pass_every_stage(self) -> None:
        self.check(IOS_PUSH, stage="ios-build", gates="ios-tests=success quality=success", mode="release",
                   marketing_version="7.2", build_number="40")
        self.check(IOS_DISPATCH, stage="ios-build", gates="ios-tests=skipped quality=skipped", mode="validate",
                   marketing_version="7.3", build_number="40", dispatch_version="7.3")
        self.check(IOS_DISPATCH, stage="ios-build", gates="ios-tests=skipped quality=skipped", mode="validate",
                   marketing_version="7.2", build_number="40")
        self.check(IOS_PUSH, stage="ios-wait-for-build", marketing_version="7.2", build_number="40",
                   upload_started_at="1800000000", wait_minutes="120", poll_seconds="30")
        self.check(IOS_PUSH, **ASC_GOOD)
        self.check(IOS_PUSH, **{**ASC_GOOD, "upload_build": "false", "submit": "false", "build_id": "",
                                "build_number": "", "listing": "true"})
        self.check(IOS_PUSH, stage="ios-github-release", gates="ios-tests=success quality=success",
                   marketing_version="7.2", uploaded_build="")
        self.check(IOS_DISPATCH, stage="ios-store-plan", marketing_version="7.2")
        self.check(ANDROID_PUSH, **PUBLISH_RELEASE)
        self.check({"GITHUB_EVENT_NAME": "workflow_dispatch"}, **PUBLISH_VALIDATE)
        self.check({"GITHUB_EVENT_NAME": "workflow_dispatch"}, stage="android-store-plan")

    def test_masked_or_empty_values_fail_closed(self) -> None:
        for key in ("marketing_version", "build_number", "gates"):
            values = dict(stage="ios-build", gates="ios-tests=success quality=success", mode="release",
                          marketing_version="7.2", build_number="40")
            values[key] = ""
            with self.subTest(key=key):
                self.fails(IOS_PUSH, "dropped", **values)
        for key in ("upload_build", "listing", "submit", "public_release", "build_id"):
            with self.subTest(key=key):
                self.fails(IOS_PUSH, "", **{**ASC_GOOD, key: ""})
        for key in ("build", "production", "play_upload", "github_release"):
            with self.subTest(key=key):
                self.fails(ANDROID_PUSH, "", **{**PUBLISH_RELEASE, key: "***"})
        self.fails(IOS_PUSH, "", stage="ios-wait-for-build", marketing_version="7.2", build_number="40",
                   upload_started_at="", wait_minutes="120", poll_seconds="30")
        self.fails(IOS_PUSH, "between 10 and 600", stage="ios-wait-for-build", marketing_version="7.2",
                   build_number="40", upload_started_at="1800000000", wait_minutes="120", poll_seconds="5")

    def test_wrong_event_and_tag_mismatch(self) -> None:
        self.fails({"GITHUB_EVENT_NAME": "pull_request"}, "tag push or a manual dispatch", stage="ios-build",
                   gates="ios-tests=success quality=success", mode="release", marketing_version="7.2",
                   build_number="40")
        self.fails(IOS_DISPATCH, "only on a tag push", **ASC_GOOD)
        self.fails({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "ios-v7.3"}, "ios-v7.3", **ASC_GOOD)
        self.fails(IOS_PUSH, "does not match", stage="ios-build", gates="ios-tests=success quality=success",
                   mode="validate", marketing_version="7.2", build_number="40")
        self.fails({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "ios-v7.3"}, "ios-v7.3", stage="ios-build",
                   gates="ios-tests=success quality=success", mode="release", marketing_version="7.2",
                   build_number="40")
        self.fails({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "ios-v7.3"}, "MARKETING_VERSION 7.2",
                   stage="ios-build", gates="ios-tests=success quality=success", mode="release",
                   marketing_version="7.3", build_number="40")
        self.fails(IOS_DISPATCH, "this dispatch validates", stage="ios-build",
                   gates="ios-tests=skipped quality=skipped", mode="validate", marketing_version="7.3",
                   build_number="40")
        self.fails(IOS_PUSH, "manual validation dispatch", stage="ios-store-plan", marketing_version="7.2")
        self.fails({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "android-v7.3"}, "android-v7.3",
                   **PUBLISH_RELEASE)

    def test_gate_rule(self) -> None:
        for gates, release in (("ios-tests=success quality=failure", True), ("ios-tests=skipped quality=skipped", True),
                               ("ios-tests=success quality=skipped", False), ("ios-tests=success", True),
                               ("ios-tests=success quality=success other=success", True),
                               ("ios-tests=success ios-tests=success", True), ("ios-tests=ok quality=success", True)):
            with self.subTest(gates=gates), self.assertRaises(stage.StageError):
                stage.gate_check(gates, ["ios-tests", "quality"], release=release)
        stage.gate_check("quality=success ios-tests=success", ["ios-tests", "quality"], release=True)
        stage.gate_check("ios-tests=skipped quality=skipped", ["ios-tests", "quality"], release=False)
        with self.assertRaises(stage.StageError) as caught:
            stage.gate_check("ios-tests=success quality=cancelled", ["ios-tests", "quality"], release=True)
        self.assertIn("quality: cancelled", str(caught.exception))

    def test_store_rules(self) -> None:
        self.fails(IOS_PUSH, "needs --upload-build", **{**ASC_GOOD, "upload_build": "false", "build_id": ""})
        self.fails(IOS_PUSH, "needs --submit", **{**ASC_GOOD, "submit": "false", "listing": "true",
                                                  "public_release": "true"})
        self.fails(IOS_PUSH, "uploads no build", **{**ASC_GOOD, "upload_build": "false", "submit": "false",
                                                    "listing": "true"})
        self.fails(IOS_PUSH, "runs only with", **{**ASC_GOOD, "submit": "false"})
        self.fails(ANDROID_PUSH, "unexpected Play track", **{**PUBLISH_RELEASE, "play_tracks": "beta"})
        self.fails(ANDROID_PUSH, "does not match the Play tracks", **{**PUBLISH_RELEASE, "production": "true"})
        self.fails(ANDROID_PUSH, "does not match the Play tracks",
                   **{**PUBLISH_RELEASE, "play_tracks": "internal,production"})
        self.fails(ANDROID_PUSH, "does not match --play-tracks", **{**PUBLISH_RELEASE, "play_tracks": ""})
        self.fails(ANDROID_PUSH, "needs a Play upload", **{**PUBLISH_RELEASE, "play_upload": "false",
                                                           "play_tracks": ""})
        self.fails(ANDROID_PUSH, "github_release=true", **{**PUBLISH_RELEASE, "github_release": "false"})
        self.fails({"GITHUB_EVENT_NAME": "workflow_dispatch"}, "every store operation off",
                   **{**PUBLISH_VALIDATE, "listing": "true"})
        self.fails({"GITHUB_EVENT_NAME": "workflow_dispatch"}, "build=true", **{**PUBLISH_VALIDATE, "build": "false"})


class ToolCheckTests(unittest.TestCase):
    def test_identity(self) -> None:
        quiet(stage.tool_check, Path("."), SHA, "", head=SHA)
        quiet(stage.tool_check, Path("."), SHA, SHA, job="ios-build", head=SHA)
        with self.assertRaisesRegex(stage.StageError, "not this workflow's commit"):
            stage.tool_check(Path("."), SHA, "", head=OTHER_SHA)
        with self.assertRaisesRegex(stage.StageError, "40-hex"):
            stage.tool_check(Path("."), "abc", "", head="abc")
        with self.assertRaisesRegex(stage.StageError, "same store-release commit"):
            stage.tool_check(Path("."), SHA, OTHER_SHA, head=SHA)
        with self.assertRaisesRegex(stage.StageError, "dropped"):
            stage.tool_check(Path("."), SHA, "", job="ios-build", head=SHA)
        quiet(stage.tool_check, Path("."), SHA, "", job="ios-prepare", head=SHA)

    def test_it_reads_the_real_checkout(self) -> None:
        head = subprocess.run(["git", "-C", str(TOOL_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
        if head.returncode != 0:
            self.skipTest("needs a git checkout")  # pragma: no cover - CI always has one
        out = io.StringIO()
        with redirect_stdout(out):
            stage.tool_check(TOOL_ROOT, head.stdout.strip(), "")
        self.assertIn(f"({head.stdout.strip()})", out.getvalue())


class SecretsCheckTests(unittest.TestCase):
    def test_names_only(self) -> None:
        env = {"HAS_A": "true", "HAS_B": "false", "B": "the value must never be read or printed"}
        out = io.StringIO()
        with redirect_stdout(out):
            stage.secrets_check(["A"], environ=env)
        self.assertEqual(out.getvalue(), "secrets present: A\n")
        with self.assertRaises(stage.StageError) as caught:
            stage.secrets_check(["A", "B"], environ=env)
        self.assertIn("missing secret(s): B", str(caught.exception))
        self.assertNotIn("never be read", str(caught.exception))
        with self.assertRaisesRegex(stage.StageError, "no HAS_ presence flag for: C"):
            stage.secrets_check(["C"], environ=env)
        with self.assertRaises(stage.StageError):
            stage.secrets_check(["bad name"], environ=env)


class ConfigExportTests(unittest.TestCase):
    def test_exports_per_job(self) -> None:
        cfg = config.load(FUD_AI_TOML)
        with tempfile.NamedTemporaryFile("r", suffix=".env") as handle:
            lines = quiet(stage.config_export, cfg, "ios-build", environ={"GITHUB_ENV": handle.name})
            written = Path(handle.name).read_text()
        self.assertEqual(written, "\n".join(lines) + "\n")
        self.assertEqual(lines, [
            "STORE_RELEASE_CONFIG=store/store-release.toml", "STORE_RELEASE_SLUG=fudai", "STORE_RELEASE_APP_NAME=Fud AI", "STORE_RELEASE_IOS_TAG_PREFIX=ios-v",
            "STORE_RELEASE_IOS_BUNDLE_ID=com.apoorvdarshan.calorietracker", "STORE_RELEASE_IOS_TEAM_ID=23RV7FYH36",
            "STORE_RELEASE_IOS_PROJECT=ios/calorietracker.xcodeproj", "STORE_RELEASE_IOS_SCHEME=calorietracker",
            "STORE_RELEASE_IOS_LOCALE=en-US", "STORE_RELEASE_IOS_FLOW=standard", "STORE_RELEASE_IOS_VERIFY_IPA=false",
            "STORE_RELEASE_IOS_SOURCE_ARCHIVE=false",
        ])
        android = quiet(stage.config_export, cfg, "android-publish", environ={})
        self.assertIn("STORE_RELEASE_ANDROID_TASKS=:app:bundleRelease :app:assembleRelease", android)
        self.assertIn("STORE_RELEASE_ANDROID_AAB=android/app/build/outputs/bundle/release/app-release.aab", android)
        self.assertIn("STORE_RELEASE_ANDROID_KEYSTORE_PROPERTIES=android/keystore.properties", android)
        self.assertFalse([line for line in android if "_IOS_" in line])
        both = quiet(stage.config_export, cfg, "checks", environ={})
        self.assertTrue([line for line in both if "_IOS_" in line] and [line for line in both if "_ANDROID_" in line])
        self.assertIn("STORE_RELEASE_HAS_IOS=true", both)
        self.assertIn("STORE_RELEASE_HAS_ANDROID=true", both)
        # Store checks skip the steps of a platform the app does not have.
        data = fud_ai_data()
        del data["android"]
        ios_only = quiet(stage.config_export, config.parse(data), "checks", environ={})
        self.assertIn("STORE_RELEASE_HAS_IOS=true", ios_only)
        self.assertIn("STORE_RELEASE_HAS_ANDROID=false", ios_only)
        self.assertFalse([line for line in ios_only if "_ANDROID_" in line and "HAS_" not in line])
        self.assertFalse([line for line in lines + android if "_HAS_" in line], "only the checks job needs them")

    def test_the_checks_job_learns_which_tables_the_config_has(self) -> None:
        cfg = config.parse(fud_ai_data())
        lines = quiet(stage.config_export, cfg, "checks", environ={})
        tables = [line for line in lines if re.match(r"STORE_RELEASE_HAS_(IOS|ANDROID)_", line)]
        self.assertEqual(tables, [f"STORE_RELEASE_HAS_{name.upper().replace('.', '_')}=true"
                                  for name in stage.OPTIONAL_TABLES])
        data = fud_ai_data()
        for name in ("listing", "whats_new", "screenshots", "creative", "products"):
            del data["ios"][name]
        del data["android"]["screenshots"]
        lines = quiet(stage.config_export, config.parse(data), "checks", environ={})
        self.assertIn("STORE_RELEASE_HAS_IOS_LISTING=false", lines)
        self.assertIn("STORE_RELEASE_HAS_IOS_PRODUCTS=false", lines)
        self.assertIn("STORE_RELEASE_HAS_ANDROID_LISTING=true", lines)
        self.assertIn("STORE_RELEASE_HAS_ANDROID_SCREENSHOTS=false", lines)

    def test_unsafe_values_and_missing_platforms_fail(self) -> None:
        data = fud_ai_data()
        data["ios"]["project"] = "ios/$(evil).xcodeproj"
        with self.assertRaisesRegex(stage.StageError, "not safe to export"):
            quiet(stage.config_export, config.parse(data), "ios-build", environ={})
        data = fud_ai_data()
        del data["ios"]
        with self.assertRaisesRegex(stage.StageError, r"\[ios\]"):
            quiet(stage.config_export, config.parse(data), "ios-build", environ={})


class FakeGh:
    def __init__(self, tags: list[str], *, exists: bool = False, fail: str | None = None) -> None:
        self.tags = tags
        self.exists = exists
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "release", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"tagName": tag} for tag in self.tags]), "")
        if argv[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(argv, 0 if self.exists else 1, "", "")
        if self.fail and argv[2] == self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls if call[2] not in ("list", "view")]


class GithubReleaseTests(unittest.TestCase):
    REPO = {"GITHUB_REPOSITORY": "apoorvdarshan/fud-ai"}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = support_apps.fixture_app("fud-ai", Path(tmp.name) / "app")
        self.notes = Path(tmp.name) / "notes.md"
        self.notes.write_text("Shipped.\n")
        self.empty = Path(tmp.name) / "empty.md"
        self.empty.write_text("")

    def release(self, gh: FakeGh, **kwargs) -> list[list[str]]:
        quiet(github.release, self.app.config, self.app.root, run=gh, environ=self.REPO, **kwargs)
        return gh.writes()

    def test_ios_create_with_and_without_notes(self) -> None:
        gh = FakeGh(["ios-v7.1.2", "android-v7.2", "ios-v7.1"])
        self.assertEqual(self.release(gh, platform="ios", tag="ios-v7.2", version="7.2", uploaded_build="40",
                                      notes_file=str(self.notes)),
                         [["gh", "release", "create", "ios-v7.2", "--repo", "apoorvdarshan/fud-ai", "--verify-tag",
                           "--title", "iOS 7.2 (build 40)", "--notes-file", str(self.notes), "--latest"]])
        gh = FakeGh(["ios-v7.1.2"])
        self.assertEqual(self.release(gh, platform="ios", tag="ios-v7.2", version="7.2", uploaded_build="",
                                      notes_file=str(self.empty)),
                         [["gh", "release", "create", "ios-v7.2", "--repo", "apoorvdarshan/fud-ai", "--verify-tag",
                           "--title", "iOS 7.2 (build 39)", "--generate-notes", "--latest"]])
        self.assertEqual(gh.calls[0], ["gh", "release", "list", "--repo", "apoorvdarshan/fud-ai", "--limit", "1000",
                                       "--exclude-drafts", "--json", "tagName"])

    def test_ios_edit_and_latest_only_for_the_highest(self) -> None:
        gh = FakeGh(["ios-v7.3", "ios-v7.2"], exists=True)
        self.assertEqual(self.release(gh, platform="ios", tag="ios-v7.2", version="7.2", uploaded_build="40",
                                      notes_file=str(self.notes)),
                         [["gh", "release", "edit", "ios-v7.2", "--repo", "apoorvdarshan/fud-ai", "--title",
                           "iOS 7.2 (build 40)", "--notes-file", str(self.notes), "--draft=false",
                           "--prerelease=false", "--latest=false"]])
        gh = FakeGh(["ios-v7.2", "ios-v7.10"], exists=False)
        self.assertEqual(self.release(gh, platform="ios", tag="ios-v7.2", version="7.2", uploaded_build="40",
                                      notes_file="")[0][-1], "--latest=false")

    def test_highest_rule(self) -> None:
        self.assertTrue(github.is_highest("ios-v7.2", "ios-v", ["ios-v7.1.9", "ios-v7.2", "android-v9.0", "ios-vX"]))
        self.assertFalse(github.is_highest("ios-v7.2", "ios-v", ["ios-v7.2.0"]))
        self.assertFalse(github.is_highest("ios-v7.2", "ios-v", ["ios-v7.10"]))
        self.assertFalse(github.is_highest("ios-vbad", "ios-v", []))

    def test_android_kinds(self) -> None:
        dist = self.app.root / "dist"
        base = ["--repo", "apoorvdarshan/fud-ai"]
        assets = [str(dist / f"fudai-v7.2{suffix}") for suffix in (".aab", ".aab.sigstore.json", ".apk",
                                                                   ".apk.sigstore.json")]
        title = ["--title", "Android 7.2 (versionCode 42)"]
        for tags, production, kind in (
            (["android-v7.1"], "true", ["--prerelease=false", "--latest"]),
            (["android-v7.3"], "true", ["--prerelease=false", "--latest=false"]),
            (["android-v7.1"], "false", ["--prerelease", "--latest=false"]),
        ):
            gh = FakeGh(tags)
            with self.subTest(tags=tags, production=production):
                self.assertEqual(self.release(gh, platform="android", tag="android-v7.2", production=production,
                                              assets_dir=str(dist), notes_file=str(self.notes)),
                                 [["gh", "release", "create", "android-v7.2", *base, "--verify-tag", *title,
                                   "--notes-file", str(self.notes), *kind, *assets]])
        gh = FakeGh(["android-v7.1"], exists=True)
        self.assertEqual(self.release(gh, platform="android", tag="android-v7.2", production="false",
                                      assets_dir=str(dist), notes_file=""),
                         [["gh", "release", "upload", "android-v7.2", *base, "--clobber", *assets],
                          ["gh", "release", "edit", "android-v7.2", *base, *title, "--prerelease", "--latest=false",
                           "--draft=false"]])

    def test_failures_and_bad_input(self) -> None:
        with self.assertRaisesRegex(github.GithubError, "boom"):
            self.release(FakeGh([], fail="create"), platform="ios", tag="ios-v7.2", version="7.2",
                         uploaded_build="40", notes_file="")
        with self.assertRaisesRegex(github.GithubError, "--production"):
            self.release(FakeGh([]), platform="android", tag="android-v7.2", production="", assets_dir="x",
                         notes_file="")
        with self.assertRaisesRegex(github.GithubError, "GITHUB_REPOSITORY"):
            github.release(self.app.config, self.app.root, platform="ios", tag="ios-v7.2", notes_file="",
                           run=FakeGh([]), environ={})

    def test_stage_android_assets_and_titles(self) -> None:
        build = self.app.root / "android/app/build/outputs"
        (build / "bundle/release").mkdir(parents=True)
        (build / "apk/release").mkdir(parents=True)
        (build / "bundle/release/app-release.aab").write_bytes(b"aab")
        (build / "apk/release/app-release.apk").write_bytes(b"apk")
        out = self.app.root / "dist"
        copied = quiet(github.stage_android_assets, self.app.config, self.app.root, tag="android-v7.2", out=out)
        self.assertEqual([p.name for p in copied], ["fudai-v7.2.aab", "fudai-v7.2.apk"])
        self.assertEqual((out / "fudai-v7.2.aab").read_bytes(), b"aab")
        example = config.load(EXAMPLE_TOML)
        self.assertEqual(github.android_asset_base(example, self.app.root, "android-v7.2")[0],
                         "example-android-7.2-42")
        self.assertEqual(github.ios_title(example, self.app.root, version="1.0", uploaded_build="5"), "Example 1.0")
        with self.assertRaisesRegex(github.GithubError, "not an android-v"):
            github.android_asset_base(self.app.config, self.app.root, "ios-v7.2")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class YankedTests(unittest.TestCase):
    LIST = "# Yanked versions\n\n<!-- - vX.Y.Z — YYYY-MM-DD — reason (use vX.Y.Z+1) -->\n- v0.1.0 — 2026-11-01 — bad pin (use v0.1.1)\n"

    def opener(self, text: str | None = None, *, error: Exception | None = None):
        seen = []

        def open_(request, timeout=None):
            seen.append(request)
            if error is not None:
                raise error
            return FakeResponse(text.encode())

        return open_, seen

    def env(self) -> dict:
        return {"TOOL_REPOSITORY": "apoorvdarshan/store-release", "GH_TOKEN": "tok-123",
                "GITHUB_API_URL": "https://api.github.com"}

    def test_parsing(self) -> None:
        self.assertEqual(yanked.listed_versions(self.LIST), {"0.1.0"})
        self.assertEqual(yanked.listed_versions("- v1.2.3 x\n  - v9.9.9\n- 2.0.0\n- v1.2.30\n"), {"1.2.3", "1.2.30"})

    def test_listed_versions_fail_or_warn(self) -> None:
        opener, seen = self.opener(self.LIST)
        with self.assertRaisesRegex(yanked.YankedError, "v0.1.0 is yanked"):
            quiet(yanked.check, warn_only=False, environ=self.env(), opener=opener, version="0.1.0")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(yanked.check(warn_only=True, environ=self.env(), opener=opener, version="0.1.0"), "warning")
        self.assertIn("::warning::store-release v0.1.0 is yanked", out.getvalue())
        self.assertEqual(quiet(yanked.check, warn_only=False, environ=self.env(), opener=opener, version="0.1.1"), "ok")
        request = seen[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/apoorvdarshan/store-release/contents/docs/yanked.md?ref=main")
        self.assertNotIn("tok-123", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer tok-123")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github.raw")

    def test_fetch_errors(self) -> None:
        opener, _ = self.opener(error=urllib.error.URLError("offline"))
        with self.assertRaisesRegex(yanked.YankedError, "could not read the yanked list; re-run"):
            quiet(yanked.check, warn_only=False, environ=self.env(), opener=opener)
        self.assertEqual(quiet(yanked.check, warn_only=True, environ=self.env(), opener=opener), "warning")
        with self.assertRaisesRegex(yanked.YankedError, "owner/name"):
            quiet(yanked.check, warn_only=False, environ={**self.env(), "TOOL_REPOSITORY": "bad repo"}, opener=opener)


class ReleaseNoteHeadingTests(unittest.TestCase):
    def run_notes(self, text: str | None, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            if text is not None:
                (Path(root) / "RELEASE_NOTES.md").write_text(text)
            with mock.patch.object(release_notes, "ROOT", Path(root)), redirect_stdout(out), redirect_stderr(err):
                try:
                    release_notes.main(list(args))
                except SystemExit as exc:
                    return exc.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_check_headings(self) -> None:
        code, out, _ = self.run_notes("## Unreleased\n\n## ios-v7.2\n\nA\n\n## android-v7.2\n\nB\n", "--check-headings")
        self.assertEqual(code, 0)
        self.assertIn("every ## heading appears once", out)
        code, _, err = self.run_notes("## ios-v7.2\n\nA\n\n## v1\n\n## ios-v7.2\n\nB\n", "--check-headings")
        self.assertEqual(code, 1)
        self.assertIn("repeats these ## headings: ios-v7.2", err)
        self.assertEqual(self.run_notes(None, "--check-headings")[0], 0)
        self.assertEqual(self.run_notes("", "--check-headings", "ios-v7.2")[0], 2)
        self.assertEqual(self.run_notes("")[0], 2)


class NotConfiguredTests(unittest.TestCase):
    def test_a_module_used_before_configure_fails(self) -> None:
        code = ("from store_release.ios import release\n"
                "try:\n    release.single_project_value('MARKETING_VERSION')\n"
                "except RuntimeError as exc:\n    print(exc)\n")
        result = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(TOOL_ROOT)!r})\n" + code],
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "store-release is not configured for an app", result.stderr)


class CliTests(unittest.TestCase):
    def test_usage_and_preconditions(self) -> None:
        self.assertEqual(run_cli([])[0], 2)
        self.assertEqual(run_cli(["--bogus", "x"])[0], 2)
        code, _, err = run_cli(["catalog", "check"])
        self.assertEqual(code, 2)
        self.assertIn("--app-root", err)
        code, _, err = run_cli(["--app-root", "/nonexistent-app", "catalog", "check"])
        self.assertEqual(code, 1)
        self.assertIn("is not a folder", err)
        self.assertEqual(run_cli(["--help"])[0], 0)

    def test_config_check_and_paths(self) -> None:
        app = support_apps.default_app()
        code, out, err = run_cli(["--app-root", str(app.root), "config", "check"])
        self.assertEqual(code, 0, err)
        self.assertIn("Fud AI (ios, android)", out)
        code, out, err = run_cli(["--app-root", str(app.root), "paths", "--job", "ios-wait-for-build"])
        self.assertEqual((code, out), (0, "/store/store-release.toml\n"), err)
        code, _, err = run_cli(["--app-root", str(app.root), "--config", "store/missing.toml", "config", "check"])
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)

    def test_text_commands_need_out(self) -> None:
        app = support_apps.default_app()
        code, _, err = run_cli(["--app-root", str(app.root), "listing", "prepare", "--platform", "ios"])
        self.assertEqual(code, 2)
        self.assertIn("needs --out", err)
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_cli(["--app-root", str(app.root), "whats-new", "prepare", "--tag", "ios-v7.2",
                                      "--out", tmp])
            self.assertEqual(code, 0, err)
            self.assertEqual((Path(tmp) / "ios/en-US/whats_new.txt").read_text(), "• Faster logging\n- Fixes\n")
            self.assertEqual((Path(tmp) / "play/whatsnew-en-US").read_text(), "• Faster logging")

    def test_secrets_and_print_switches_need_no_app(self) -> None:
        code, out, _ = run_cli(["secrets", "check", "A"], env={"HAS_A": "true"})
        self.assertEqual((code, out), (0, "secrets present: A\n"))
        code, out, _ = run_cli(["print-switches", "--platform", "ios"], env={"IOS_SUBMIT_REVIEW": "maybe"})
        self.assertEqual(code, 0)
        self.assertIn("IOS_SUBMIT_REVIEW=INVALID 'maybe'", out)


class CallerRuntimeTests(unittest.TestCase):
    """caller check --runtime (stdlib): pins and the release queue of the running caller."""

    SHA = "c" * 40

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.callers = callers
        cfg = config.load(FUD_AI_TOML)
        self.text = callers.render(cfg, "ios", sha=self.SHA, version="0.1.0")
        self.path = self.root / ".github/workflows/ios-release.yml"
        self.path.parent.mkdir(parents=True)
        self.env = {"TOOL_SHA": self.SHA, "TOOL_REPOSITORY": "apoorvdarshan/store-release",
                    "GITHUB_REPOSITORY": "apoorvdarshan/fud-ai",
                    "GITHUB_WORKFLOW_REF": "apoorvdarshan/fud-ai/.github/workflows/ios-release.yml@refs/tags/ios-v7.2"}
        self.tags = TagApi({f"v{__version__}": self.SHA})

    def check(self, text: str, *, tags: TagApi | None = None) -> str:
        self.path.write_text(text)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.callers.runtime_check(self.root, "ios", environ=self.env, opener=tags or self.tags)
        return out.getvalue()

    def test_the_rendered_caller_passes(self) -> None:
        out = self.check(self.text)
        self.assertIn(f"the v{__version__} tag points at {self.SHA}", out)
        self.assertNotIn("::warning", out)

    def test_wrong_or_mixed_pins_fail(self) -> None:
        mixed = self.text.replace(f"ios-build.yml@{self.SHA}", f"ios-build.yml@{'d' * 40}")
        with self.assertRaisesRegex(self.callers.CallerError, "ios-build.yml@dddd"):
            self.check(mixed)
        with self.assertRaisesRegex(self.callers.CallerError, "pinned to"):
            self.check(self.text.replace(self.SHA, "e" * 40))
        with self.assertRaisesRegex(self.callers.CallerError, "pinned to"):
            self.check(self.text.replace(f"ios-prepare.yml@{self.SHA}", "ios-prepare.yml@v0.1.0"))
        with self.assertRaisesRegex(self.callers.CallerError, "calls no"):
            self.check("name: x\non: push\njobs: {}\n")

    def test_the_release_queue_is_required(self) -> None:
        with self.assertRaisesRegex(self.callers.CallerError, "concurrency"):
            self.check(self.text.replace("  queue: max\n", ""))
        with self.assertRaisesRegex(self.callers.CallerError, "concurrency"):
            self.check(self.text.replace("|| github.run_id }}", "|| github.sha }}", 1))
        with self.assertRaisesRegex(self.callers.CallerError, "concurrency"):
            self.check(self.text.replace("cancel-in-progress: false", "cancel-in-progress: true"))
        # Whitespace inside a line does not matter.
        self.check(self.text.replace("  cancel-in-progress: false", "  cancel-in-progress:   false"))

    def test_the_tool_identity_is_required(self) -> None:
        for key, value in (("TOOL_SHA", "abc"), ("TOOL_REPOSITORY", "")):
            env = {**self.env, key: value}
            self.path.write_text(self.text)
            with self.subTest(key=key), self.assertRaises(self.callers.CallerError):
                quiet(self.callers.runtime_check, self.root, "ios", environ=env, opener=self.tags)

    def test_a_pin_the_release_tag_does_not_name_is_only_a_warning(self) -> None:
        # A release never depends on mutable remote state: Store checks fail on these, a release run goes on.
        cases = {
            "untagged": (TagApi({}), f"has no release tag v{__version__}"),
            "another commit": (TagApi({f"v{__version__}": "d" * 40}), f"v{__version__} is {'d' * 40}, not the pinned"),
            "unreadable": (TagApi(error=urllib.error.URLError("offline")), "could not read"),
        }
        for name, (tags, fragment) in cases.items():
            with self.subTest(case=name):
                out = self.check(self.text, tags=tags)
                self.assertIn("::warning title=store-release pin::", out)
                self.assertIn(fragment, out)


class ReleaseTagTests(unittest.TestCase):
    """The release tag lookup behind pins check, caller check and callers render (fake GitHub API)."""

    REPO = "apoorvdarshan/store-release"

    def test_lightweight_and_annotated_tags_resolve_to_their_commit(self) -> None:
        for annotated in (False, True):
            tags = TagApi({"v0.1.0": SHA}, annotated=annotated)
            with self.subTest(annotated=annotated):
                self.assertEqual(callers.release_tag_commit(self.REPO, "0.1.0", environ={}, opener=tags), SHA)
                self.assertEqual(len(tags.requests), 2 if annotated else 1)
        self.assertIsNone(callers.release_tag_commit(self.REPO, "0.2.0", environ={}, opener=TagApi({"v0.1.0": SHA})))

    def test_the_request(self) -> None:
        tags = TagApi({"v0.1.0": SHA}, repository=self.REPO)
        callers.release_tag_commit(self.REPO, "0.1.0", environ={"GH_TOKEN": "tok-123"}, opener=tags)
        request = tags.requests[0]
        self.assertEqual(request.full_url, f"https://api.github.com/repos/{self.REPO}/git/ref/tags/v0.1.0")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok-123")
        self.assertNotIn("tok-123", request.full_url)
        unauthenticated = TagApi({"v0.1.0": SHA})
        callers.release_tag_commit(self.REPO, "0.1.0", environ={}, opener=unauthenticated)
        self.assertIsNone(unauthenticated.requests[0].get_header("Authorization"))
        seen = []

        def enterprise(request, timeout=None):
            seen.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

        callers.release_tag_commit(self.REPO, "0.1.0", environ={"GITHUB_API_URL": "https://ghe.example/api/v3/"},
                                   opener=enterprise)
        self.assertEqual(seen, [f"https://ghe.example/api/v3/repos/{self.REPO}/git/ref/tags/v0.1.0"])

    def test_failures_fail_closed(self) -> None:
        def answer(body: bytes):
            return lambda request, timeout=None: FakeResponse(body)

        cases = [
            (TagApi(error=urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b""))), "HTTP 500"),
            (TagApi(error=urllib.error.URLError("offline")), "offline"),
            (answer(b"not json"), "could not read"),
            (answer(b"[]"), "unexpected response"),
            (answer(json.dumps({"object": {"type": "tree", "sha": SHA}}).encode()), "does not resolve to a commit"),
            (answer(json.dumps({"object": {"type": "commit", "sha": "abc"}}).encode()), "does not resolve to a commit"),
            # An annotated tag pointing at itself never reaches a commit.
            (answer(json.dumps({"object": {"type": "tag", "sha": SHA}}).encode()), "does not resolve to a commit"),
        ]
        for opener, fragment in cases:
            with self.subTest(fragment=fragment), self.assertRaisesRegex(callers.CallerError, fragment):
                callers.release_tag_commit(self.REPO, "0.1.0", environ={}, opener=opener)
        with self.assertRaisesRegex(callers.CallerError, "owner/name"):
            callers.release_tag_commit("bad repo", "0.1.0", environ={}, opener=TagApi())
        with self.assertRaisesRegex(callers.CallerError, "must look like 0.1.0"):
            callers.release_tag_commit(self.REPO, "v0.1", environ={}, opener=TagApi())

    def test_check_release_tag(self) -> None:
        self.assertEqual(callers.check_release_tag(SHA, self.REPO, environ={}, opener=TagApi({"v0.1.0": SHA}),
                                                   version="0.1.0"), f"the v0.1.0 tag points at {SHA}")
        with self.assertRaisesRegex(callers.CallerError, "has no release tag v0.1.0: pin the commit of a store-release"):
            callers.check_release_tag(SHA, self.REPO, environ={}, opener=TagApi({}), version="0.1.0")
        with self.assertRaisesRegex(callers.CallerError, f"v0.1.0 is {OTHER_SHA}, not the pinned {SHA}"):
            callers.check_release_tag(SHA, self.REPO, environ={}, opener=TagApi({"v0.1.0": OTHER_SHA}),
                                      version="0.1.0")


FORBIDDEN = ("calorietracker", "fudai", "FudAI", "Fud AI", "6758935726", "23RV7FYH36",
             "35d739f426248c3c04f344ffaaf972dabc67b9aaa64f49143ef8f3f4b1f704e0")
# The apps that run on store-release (examples/<app>/): none of their identities may reach the shared code.
USED_BY = ("fud-ai", "scowld", "verceltics", "abysm", "pcm-notes", "quit-all")
# store-release's own CI, release and probe workflows name its repository and the canary app by design.
OWN_WORKFLOWS = ("ci.yml", "codeql.yml", "dependency-review.yml", "fuzz.yml", "probe-inner.yml", "probe-rerun.yml",
                 "release.yml", "scorecard.yml")
# The one place the package names its own repository: the default for callers render and init.
OWN_REPOSITORY_LINE = 'DEFAULT_REPOSITORY = "apoorvdarshan/store-release"'


def app_identities() -> list[str]:
    """Every app's name, bundle ids, App Store Connect app id, team, package, upload certificate and product prefix."""
    values = set(FORBIDDEN)
    for app in USED_BY:
        data = tomllib.loads((TOOL_ROOT / "examples" / app / "store-release.toml").read_text())
        values.add(data["app"]["name"])
        ios, android, catalog_table = data.get("ios", {}), data.get("android", {}), data.get("catalog", {})
        values |= {ios[key] for key in ("bundle_id", "asc_app_id", "team_id") if key in ios}
        values |= set(ios.get("build", {}).get("extensions", []))
        values |= {android[key] for key in ("package", "upload_cert_sha256") if key in android}
        if "id_prefix" in catalog_table:
            values.add(catalog_table["id_prefix"].rstrip("."))
    return sorted(values)


def shared_files() -> list[Path]:
    """The package (without the ported flows, which are the apps' own scripts), the reusable workflows and
    the job-setup action."""
    files = [path for path in sorted((TOOL_ROOT / "store_release").rglob("*.py"))
             if "flows" not in path.relative_to(TOOL_ROOT / "store_release").parts[:1]]
    files += [path for path in sorted((TOOL_ROOT / ".github/workflows").glob("*.yml")) if path.name not in OWN_WORKFLOWS]
    files += [path for path in sorted((TOOL_ROOT / ".github/actions").rglob("*")) if path.is_file()]
    return files


class NoHardCodedAppTests(unittest.TestCase):
    """The package, the reusable workflows and the job-setup action never name an app or its account."""

    def test_no_fud_ai_values(self) -> None:
        files = sorted((TOOL_ROOT / "store_release").rglob("*.py"))
        files += sorted((TOOL_ROOT / ".github/workflows").glob("*.yml"))
        files += sorted((TOOL_ROOT / ".github/actions").rglob("*"))
        found = []
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for word in FORBIDDEN:
                if word in text:
                    found.append(f"{path.relative_to(TOOL_ROOT)}: {word}")
        self.assertEqual(found, [])

    def test_no_value_of_any_app_that_uses_store_release(self) -> None:
        identities = app_identities()
        self.assertGreater(len(identities), 20)
        found = []
        for path in shared_files():
            text = path.read_text(encoding="utf-8")
            for value in identities:
                if value in text:
                    found.append(f"{path.relative_to(TOOL_ROOT)}: {value}")
        self.assertEqual(found, [])

    def test_no_account_is_named_but_store_release_itself(self) -> None:
        found = []
        for path in shared_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "apoorvdarshan" in line and line.strip() != OWN_REPOSITORY_LINE:
                    found.append(f"{path.relative_to(TOOL_ROOT)}:{number}: {line.strip()}")
                if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", line) and "noreply" not in line:
                    if not re.search(r"@[0-9a-f]{40}|@v?[0-9]|@main|@refs|@\$|@\{", line):
                        found.append(f"{path.relative_to(TOOL_ROOT)}:{number}: an email address")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
