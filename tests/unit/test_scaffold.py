"""`run.py init` (store_release.scaffold): the answers, the config and callers it writes, the pin and the
templates in examples/templates/, which are exactly what it writes for their answers."""
from __future__ import annotations

import io
import re
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from store_release import __version__, callers, cli, config, scaffold
from support import apps as support_apps
from support import trees
from support.tag_fakes import TagApi

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = support_apps.TOOL_ROOT
TEMPLATES = TOOL_ROOT / "examples" / "templates"
SHA = "c4" * 20
CERT = "ab" * 32
IOS = ["--name", "My App", "--bundle-id", "com.example.myapp", "--asc-app-id", "1234567890", "--team-id",
       "ABCDE12345", "--project", "ios/MyApp.xcodeproj"]
ANDROID = ["--package", "com.example.myapp", "--upload-cert-sha256", CERT, "--app-repository", "someone/my-app"]


def run_init(root: Path, *args: str, opener=None, ask=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if opener is None and ask is None:
            code = cli.main(["--app-root", str(root), "init", "--no-input", *args])
        else:
            try:
                code = scaffold.main(root, ["--no-input", *args] if ask is None else list(args), ask=ask,
                                     environ={}, opener=opener)
            except config.ConfigError as exc:
                err.write(f"error: {exc}\n")
                code = 1
    return code, out.getvalue(), err.getvalue()


def app_tree(root: Path, *, generated_plist: bool = False) -> Path:
    return trees.write_new_app_tree(root, generated_plist=generated_plist)


class TemplateTests(unittest.TestCase):
    def test_the_templates_are_what_init_writes(self) -> None:
        self.assertEqual(sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir()), sorted(scaffold.TEMPLATES))
        for name in scaffold.TEMPLATES:
            expected = scaffold.template_files(name)
            committed = {p.relative_to(TEMPLATES / name).as_posix(): p.read_text(encoding="utf-8")
                         for p in sorted((TEMPLATES / name).rglob("*")) if p.is_file()}
            with self.subTest(template=name):
                self.assertEqual(sorted(committed), sorted(expected))
                for relative, text in expected.items():
                    self.assertEqual(committed[relative], text, relative)

    def test_the_templates_use_placeholders_only(self) -> None:
        for name in scaffold.TEMPLATES:
            cfg = config.load(TEMPLATES / name / "store-release.toml")
            with self.subTest(template=name):
                self.assertEqual((cfg.app.name, cfg.app.slug), ("Example App", "example-app"))
                if cfg.ios is not None:
                    self.assertEqual((cfg.ios.bundle_id, cfg.ios.asc_app_id, cfg.ios.team_id),
                                     ("com.example.app", "1234567890", "ABCDE12345"))
                    self.assertEqual(cfg.ios.flow, "standard")
                if cfg.android is not None:
                    self.assertEqual((cfg.android.package, cfg.android.upload_cert_sha256),
                                     ("com.example.app", "0" * 64))
                for text in (TEMPLATES / name / "callers").glob("*.yml"):
                    refs = callers.store_release_refs(text.read_text(), callers.DEFAULT_REPOSITORY)
                    self.assertTrue(refs)
                    self.assertEqual({(ref, rest.strip()) for _, _, ref, rest in refs},
                                     {(scaffold.PLACEHOLDER_SHA, f"# v{__version__}")})


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "app"
        self.root.mkdir()

    def test_every_platform_writes_a_config_that_loads_and_pinned_callers(self) -> None:
        cases = {"ios": (["--platform", "ios", *IOS], ["ios-release.yml", "store-checks.yml"]),
                 "android": (["--platform", "android", "--name", "My App", *ANDROID],
                             ["android-release.yml", "store-checks.yml"]),
                 "all": (["--platform", "all", *IOS, *ANDROID],
                         ["android-release.yml", "ios-release.yml", "store-checks.yml"])}
        for platform, (args, workflows) in cases.items():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, out, err = run_init(root, *args, "--sha", SHA, "--allow-untagged")
                self.assertEqual(code, 0, err)
                cfg = config.load_for_app(root)
                self.assertEqual((cfg.app.name, cfg.app.slug), ("My App", "my-app"))
                self.assertEqual(cfg.ios is not None, platform in ("ios", "all"))
                self.assertEqual(cfg.android is not None, platform in ("android", "all"))
                both = platform == "all"
                if cfg.ios is not None:
                    self.assertEqual((cfg.ios.tag_prefix, cfg.ios.scheme, cfg.ios.app_target, cfg.ios.build.checkout),
                                     ("ios-v" if both else "v", "MyApp", "MyApp", ("/ios/",)))
                    self.assertEqual([job.id for job in cfg.ios.gate_jobs], ["ios-tests"])
                if cfg.android is not None:
                    self.assertEqual((cfg.android.tag_prefix, cfg.android.upload_cert_sha256),
                                     ("android-v" if both else "v", CERT))
                    self.assertEqual(cfg.android.build.signing, "keystore-properties")
                found = sorted(p.name for p in (root / ".github/workflows").glob("*.yml"))
                self.assertEqual(found, workflows)
                for name in workflows:
                    text = (root / ".github/workflows" / name).read_text()
                    self.assertEqual(text, callers.render(cfg, {"ios-release.yml": "ios", "android-release.yml":
                                                                "android", "store-checks.yml": "checks"}[name],
                                                          sha=SHA, version=__version__, app_repository="someone/my-app"))
                self.assertIn('patterns: ["apoorvdarshan/store-release*"]', (root / ".github/dependabot.yml").read_text())
                self.assertIn("wrote store/store-release.toml", out)
                self.assertIn("next:", out)

    def test_the_scaffolded_app_passes_config_check_files(self) -> None:
        for generated in (False, True):
            with self.subTest(generated_plist=generated), tempfile.TemporaryDirectory() as tmp:
                root = app_tree(Path(tmp), generated_plist=generated)
                code, out, err = run_init(root, "--platform", "all", *IOS, *ANDROID, "--sha", SHA, "--allow-untagged")
                self.assertEqual(code, 0, err)
                cfg = config.load_for_app(root)
                self.assertEqual(cfg.ios.info_plist, None if generated else "ios/MyApp/Info.plist")
                checked = config.check_files(cfg, root)
                self.assertIn("ios/MyApp/Info.plist (INFOPLIST_FILE)" if not generated
                              else "MyApp: no INFOPLIST_FILE (Xcode generates the Info.plist)", checked)
                self.assertNotIn("not in", out.split("next:")[1].splitlines()[1])
                self.assertIn("has `on: workflow_call`", out)

    def test_the_pin_is_the_release_tags_commit(self) -> None:
        tag = {f"v{__version__}": SHA}
        code, out, err = run_init(self.root, "--platform", "ios", *IOS, opener=TagApi(tag))
        self.assertEqual(code, 0, err)
        self.assertIn(f"@{SHA} # v{__version__}", (self.root / ".github/workflows/ios-release.yml").read_text())
        code, _, err = run_init(self.root, "--platform", "ios", *IOS, "--force", opener=TagApi({}))
        self.assertEqual(code, 1)
        self.assertIn(f"has no release tag v{__version__}", err)
        code, _, err = run_init(self.root, "--platform", "ios", *IOS, "--force", "--sha", "d" * 40,
                                opener=TagApi(tag))
        self.assertEqual(code, 1)
        self.assertIn(f"is {SHA}, not the pinned {'d' * 40}", err)
        code, _, err = run_init(self.root, "--platform", "ios", *IOS, "--force", "--sha", "main", "--allow-untagged")
        self.assertEqual(code, 1)
        self.assertIn("not a 40-hex commit SHA", err)

    def test_existing_files_are_never_overwritten_without_force(self) -> None:
        trees.write_text(self.root, ".github/dependabot.yml", "version: 2\nupdates: []\n")
        code, out, err = run_init(self.root, "--platform", "ios", *IOS, "--sha", SHA, "--allow-untagged")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / ".github/dependabot.yml").read_text(), "version: 2\nupdates: []\n")
        self.assertNotIn("wrote .github/dependabot.yml", out)
        self.assertIn(".github/dependabot.yml exists, so it was left alone", out)
        config_text = (self.root / "store/store-release.toml").read_text()
        code, _, err = run_init(self.root, "--platform", "ios", *IOS, "--slug", "other", "--sha", SHA,
                                "--allow-untagged")
        self.assertEqual(code, 1)
        self.assertIn("init would overwrite store/store-release.toml, .github/workflows/ios-release.yml, "
                      ".github/workflows/store-checks.yml (pass --force", err)
        self.assertEqual((self.root / "store/store-release.toml").read_text(), config_text)
        code, _, err = run_init(self.root, "--platform", "ios", *IOS, "--slug", "other", "--sha", SHA,
                                "--allow-untagged", "--force")
        self.assertEqual(code, 0, err)
        self.assertIn('slug = "other"', (self.root / "store/store-release.toml").read_text())

    def test_another_config_path_reaches_the_callers(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--app-root", str(self.root), "--config", "release/store.toml", "init", "--no-input",
                             "--platform", "ios", *IOS, "--sha", SHA, "--allow-untagged"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue((self.root / "release/store.toml").is_file())
        self.assertIn('config: "release/store.toml"', (self.root / ".github/workflows/ios-release.yml").read_text())

    def test_missing_answers_are_named(self) -> None:
        code, _, err = run_init(self.root, "--platform", "all", "--name", "My App")
        self.assertEqual(code, 1)
        self.assertIn("init needs --project, --bundle-id, --asc-app-id, --team-id, --package, "
                      "--upload-cert-sha256, --app-repository", err)
        code, _, err = run_init(self.root)
        self.assertIn("init needs --platform, --name", err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_bad_answer_names_its_flag(self) -> None:
        cases = [(["--bundle-id", "nodots"], "ios.bundle_id", "--bundle-id"),
                 (["--team-id", "abc"], "ios.team_id", "--team-id"),
                 (["--asc-app-id", "12"], "ios.asc_app_id", "--asc-app-id"),
                 (["--ios-tests", ".github/workflows/build.yml"], "reserved", "--ios-tests"),
                 (["--developer-dir", "/Applications/Xcode-beta.app/Contents/Developer"], "released Xcode",
                  "--developer-dir"),
                 (["--runner", "mac os"], "ios.build.runner", "--runner")]
        for extra, fragment, flag in cases:
            with self.subTest(flag=flag):
                code, _, err = run_init(self.root, "--platform", "ios", *IOS, *extra, "--sha", SHA, "--allow-untagged")
                self.assertEqual(code, 1)
                self.assertIn(fragment, err)
                self.assertIn(f"({flag})", err)
        code, _, err = run_init(self.root, "--platform", "ios", *IOS, "--ios-tests", "tests.yml", "--sha", SHA,
                                "--allow-untagged")
        self.assertIn("--ios-tests: 'tests.yml' must be a workflow file in .github/workflows/", err)
        code, _, err = run_init(self.root, "--platform", "android", "--name", "A", *ANDROID[:2],
                                "--upload-cert-sha256", "12:34", "--app-repository", "o/r", "--sha", SHA,
                                "--allow-untagged")
        self.assertIn("(--upload-cert-sha256)", err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_answers_are_asked_for_in_a_terminal(self) -> None:
        answers = iter(["all", "Quiz Time", "", "ios/Quiz.xcodeproj", "", "", "com.example.quiz", "1234567890",
                        "ABCDE12345", "", "", "", "com.example.quiz", CERT.upper(), "", "", "", "someone/quiz"])
        asked: list[str] = []

        def ask(prompt: str) -> str:
            asked.append(prompt)
            return next(answers)

        code, out, err = run_init(self.root, "--sha", SHA, "--allow-untagged", ask=ask)
        self.assertEqual(code, 0, err)
        cfg = config.load_for_app(self.root)
        self.assertEqual((cfg.app.slug, cfg.ios.scheme, cfg.ios.bundle_id, cfg.android.upload_cert_sha256),
                         ("quiz-time", "Quiz", "com.example.quiz", CERT))
        self.assertIn("Slug (temporary file names, asset names) [quiz-time]: ", asked)
        self.assertIn("Scheme to archive [Quiz]: ", asked)

    def test_answer_helpers(self) -> None:
        self.assertEqual(scaffold.slug_for("My Great App!"), "my-great-app")
        self.assertEqual(scaffold.slug_for("日本"), "app")
        self.assertEqual(scaffold.slug_for("x" * 40), "x" * 32)
        self.assertEqual(scaffold.normalize_cert("AB:CD:" + "EF" * 30), "abcd" + "ef" * 30)
        self.assertEqual(scaffold.gate_id(".github/workflows/Unit Tests.yml".replace(" ", "_"), "k"), "unit_tests")
        self.assertEqual(scaffold.toml_str('a "b" \\ \n'), '"a \\"b\\" \\\\ \\u000a"')
        self.assertEqual(tomllib.loads(f"x = {scaffold.toml_str(chr(9) + 'é' + chr(127))}")["x"], "\té\x7f")
        for remote, expected in (("https://github.com/o/r.git", "o/r"), ("git@github.com:o/r.git", "o/r"),
                                 ("https://github.com/o/r", "o/r"), ("https://gitlab.com/o/r.git", None)):
            with self.subTest(remote=remote):
                match = scaffold.REMOTE.fullmatch(remote)
                self.assertEqual(match.group(1) if match else None, expected)

    def test_the_info_plist_is_read_from_the_project(self) -> None:
        app_tree(self.root)
        self.assertEqual(scaffold.detect_info_plist(self.root, "ios/MyApp.xcodeproj", "MyApp"), "ios/MyApp/Info.plist")
        self.assertEqual(scaffold.detect_info_plist(self.root, "ios/MyApp.xcodeproj", "Other"), "")
        with tempfile.TemporaryDirectory() as tmp:
            generated = app_tree(Path(tmp), generated_plist=True)
            self.assertEqual(scaffold.detect_info_plist(generated, "ios/MyApp.xcodeproj", "MyApp"), "")
        self.assertEqual(scaffold.detect_info_plist(self.root, "missing/App.xcodeproj", "App"), "")

    def test_the_generated_config_has_no_app_specific_value(self) -> None:
        text = scaffold.template_files("ios-and-android")["store-release.toml"]
        self.assertFalse(re.search(r"fud|calorie", text, re.I))
        self.assertEqual(re.findall(r"apoorvdarshan/[\w-]+", text), ["apoorvdarshan/store-release"])
        self.assertIn(f"/blob/v{__version__}/docs/config.md", text)


if __name__ == "__main__":
    unittest.main()
