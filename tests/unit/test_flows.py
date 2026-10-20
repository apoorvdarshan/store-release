"""The ported flows (store_release.flows): exact ports, configured from the config, routed by the CLI."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store_release import config, context, flows, github, redact, release_notes, stage
from store_release.flows import commands, port, rebind
from support import apps as support_apps
from support import clirun

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOL_ROOT / "tests"))
from flows import trees  # noqa: E402

APPS = tuple(port.FLOWS)


def upstream_dir(app: str) -> Path:
    return trees.upstream_dir(app)


class PortTests(unittest.TestCase):
    """Every flow module is exactly port() of the app's verbatim script, and the copies are the app's."""

    def test_every_flow_module_is_the_port_of_its_upstream_script(self) -> None:
        for app, (package, _sha, files) in port.FLOWS.items():
            for source, module in files.items():
                with self.subTest(app=app, module=module):
                    upstream = (upstream_dir(app) / port.upstream_name(source)).read_text(encoding="utf-8")
                    ported = (TOOL_ROOT / "store_release" / "flows" / package / f"{module}.py").read_text(
                        encoding="utf-8")
                    self.assertEqual(ported, port.port_module(app, source, upstream))

    def test_the_upstream_copies_match_their_manifest(self) -> None:
        for app in APPS:
            folder = upstream_dir(app)
            lines = [line for line in (folder / "MANIFEST.sha256").read_text().splitlines()
                     if line and not line.startswith("#")]
            listed = set()
            for line in lines:
                digest, name, _source = line.split("  ")
                listed.add(name)
                with self.subTest(app=app, name=name):
                    self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(listed, {p.name for p in folder.iterdir()
                                      if p.name not in ("MANIFEST.sha256", "__pycache__")})

    def test_the_port_only_rewrites_root_imports_and_identity(self) -> None:
        text = ("from __future__ import annotations\nimport sys\nfrom pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parent))\nimport ios_metadata  # noqa: E402\n"
                "ROOT = Path(__file__).resolve().parents[2]\nBUNDLE_ID = \"com.example.App\"\n"
                "def f():\n    import asc_release\n    return ROOT / 'x'\n")
        out = port.placeholders(port.drop_unused(port.port(text, {"ios_metadata", "asc_release"})), ("BUNDLE_ID",))
        self.assertIn("from . import ios_metadata  # noqa: E402", out)
        self.assertIn("    from . import asc_release", out)
        self.assertIn("ROOT = UNCONFIGURED_ROOT", out)
        self.assertIn("BUNDLE_ID = UNCONFIGURED_TEXT", out)
        self.assertNotIn("sys.path", out)
        self.assertNotIn("com.example.App", out)

    def test_no_app_identity_is_hard_coded_in_a_flow(self) -> None:
        identities = []
        for app in APPS:
            cfg = config.load(TOOL_ROOT / "examples" / app / "store-release.toml")
            identities += [cfg.ios.bundle_id, cfg.ios.asc_app_id]
            if cfg.android is not None:
                identities += [cfg.android.upload_cert_sha256]
        for path in sorted((TOOL_ROOT / "store_release").rglob("*.py")):
            code = [line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#")]
            for value in identities:
                with self.subTest(path=path.name, value=value):
                    self.assertFalse(any(f'"{value}"' in line for line in code), f"{path}: {value}")


def isolated_upstream(app: str, tree: Path) -> dict:
    """The app's own scripts imported from tree/scripts/ (so their ROOT is tree), isolated from sys.modules."""
    _, _, files = port.FLOWS[app]
    for source in files:
        target = tree / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upstream_dir(app) / port.upstream_name(source), target)
    saved_path, saved_modules = list(sys.path), dict(sys.modules)
    names = set(files.values())
    for name in names:
        sys.modules.pop(name, None)
    try:
        sys.path.insert(0, str(tree / "scripts"))
        sys.path.insert(0, str(tree / "scripts" / "store"))
        return {name: importlib.import_module(name) for name in files.values()}
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - set(saved_modules):
            sys.modules.pop(name, None)
        sys.modules.update({k: v for k, v in saved_modules.items() if k in names})


def comparable(value):
    if isinstance(value, type) or callable(value):
        return None
    if isinstance(value, (set, frozenset)):
        return sorted(map(repr, value))
    return repr(value)


class ConfigureTests(unittest.TestCase):
    """configure() from the example config gives each flow module exactly the app's own constants."""

    def test_every_constant_equals_the_apps_own_script(self) -> None:
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                tree = trees.build_tree(app, Path(tmp) / "app").resolve()
                upstream = isolated_upstream(app, tree)
                cfg = config.load_for_app(tree)
                saved = support_apps.snapshot()
                try:
                    context.configure(cfg, tree)
                    package = flows.flow_package(app)
                    for name, original in upstream.items():
                        ported = importlib.import_module(f"{package.__name__}.{name}")
                        renamed = port.RENAMES.get((port.FLOWS[app][0], name), {})
                        for key, value in vars(original).items():
                            if not key.isupper() or key.startswith("UNCONFIGURED_"):
                                continue
                            self.assertEqual(comparable(getattr(ported, renamed.get(key, key), "<missing>")),
                                             comparable(value), f"{app} {name}.{key}")
                        left = [n for n in rebind.unconfigured_values(
                            ported, (flows.UNCONFIGURED_ROOT, flows.UNCONFIGURED_TEXT)) if not n.startswith("UNCONF")]
                        self.assertEqual(left, [], f"{app} {name}")
                finally:
                    support_apps.restore(saved)

    def test_a_flow_used_before_configure_points_nowhere(self) -> None:
        self.assertFalse(flows.UNCONFIGURED_ROOT.exists())

    def test_rebind_recomputes_derived_values_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample_flow.py"
            source.write_text("from __future__ import annotations\nfrom pathlib import Path\n"
                              "ROOT = Path('/nowhere')\nDIR = ROOT / 'store'\nNAME = 'x'\n"
                              "def where(root: Path = ROOT) -> Path:\n    return root\n"
                              "TABLE = {'dir': DIR}\n", encoding="utf-8")
            sys.path.insert(0, tmp)
            try:
                module = importlib.import_module("sample_flow")
                rebound = rebind.rebind(module, {"ROOT": Path("/app")})
            finally:
                sys.path.remove(tmp)
                sys.modules.pop("sample_flow", None)
        self.assertEqual(module.DIR, Path("/app/store"))
        self.assertEqual(module.where(), Path("/app"))
        self.assertEqual(module.TABLE, {"dir": Path("/app/store")})
        self.assertEqual(module.NAME, "x")
        self.assertEqual(rebound, ["DIR", "where", "TABLE"])
        with self.assertRaises(AttributeError):
            rebind.rebind(module, {"NOPE": 1})


class FlowConfigTests(unittest.TestCase):
    """The config keys of the ported flows: each flow accepts exactly what it does."""

    def load(self, app: str, edit=None) -> config.Config:
        text = (TOOL_ROOT / "examples" / app / "store-release.toml").read_text()
        if edit:
            text = edit(text)
        import tomllib  # noqa: PLC0415

        return config.parse(tomllib.loads(text))

    def test_every_example_loads(self) -> None:
        for app in APPS:
            with self.subTest(app=app):
                cfg = self.load(app)
                self.assertEqual(cfg.ios.flow, app)
                self.assertTrue(cfg.ported_flow)

    def test_values_per_app(self) -> None:
        scowld, pcm, quit_all = self.load("scowld"), self.load("pcm-notes"), self.load("quit-all")
        verceltics, abysm = self.load("verceltics"), self.load("abysm")
        self.assertEqual((scowld.ios.title, scowld.ios.on_existing, scowld.ios.latest, scowld.ios.source_archive),
                         ("Scowld {version}", "upload-missing", "always", True))
        self.assertEqual(scowld.ios.screenshot_manifest, "screenshots.json")
        self.assertIsNone(scowld.android)
        self.assertEqual(pcm.ios.gate_policy, "when-uploading")
        self.assertEqual(pcm.ios.build.post_archive, ("scripts/ci/check_offline_notes.sh",))
        self.assertEqual((pcm.ios.title, pcm.ios.on_existing), ("PCM Notes {tag}{build_suffix}", "keep"))
        self.assertEqual(quit_all.ios.gate_policy, "after-prepare")
        self.assertEqual(quit_all.ios.build.extensions, ("com.apoorvdarshan.Quit-All.Widget",))
        self.assertTrue(quit_all.ios.build.verify_ipa)
        self.assertEqual(quit_all.ios.whats_new.version_file, "whats_new_version.txt")
        self.assertEqual(verceltics.ios.whats_new.source, "release-notes")
        self.assertEqual(verceltics.ios.prepare_checks, ("scripts/ci/release_gate_tests.sh",))
        self.assertEqual(verceltics.android.build.signing, "gradle-properties")
        self.assertEqual(verceltics.android.build.key_alias, "upload")
        self.assertEqual(verceltics.android.build.sdk_packages, ("platform-tools", "platforms;android-37.0"))
        self.assertEqual(verceltics.android.gate_jobs[0].inputs, (("android-release", True),))
        self.assertFalse(verceltics.android.pad_to_9_16)
        self.assertEqual(abysm.ios.gate_jobs[0].id, "quality")
        self.assertFalse(scowld.unique_headings)
        self.assertTrue(abysm.unique_headings)

    def test_a_flow_rejects_what_it_does_not_do(self) -> None:
        cases = [
            ("scowld", lambda t: t.replace('source = "files"\ndir = "store/metadata/ios"', 'source = "markdown"'),
             "ios.listing.source"),
            ("verceltics", lambda t: t.replace('[ios.whats_new]\nsource = "release-notes"', '[ios.whats_new]\nsource = "files"'),
             "ios.whats_new.source"),
            ("abysm", lambda t: t.replace('display_type = "APP_IPHONE_DUO"\ndir = "store/metadata/screenshots/ios/iphone-duo"\nrequired = false',
                                          'display_type = "APP_IPHONE_DUO"\ndir = "store/metadata/screenshots/ios/iphone-duo"\nrequired = true'),
             "the abysm flow uploads exactly these sets"),
            ("scowld", lambda t: t + '\n[ios.products]\niap_route = "submission"\n', "ios.products"),
            ("pcm-notes", lambda t: t.replace('[ios]\n', '[ios]\nexport_compliance = "NO"\n', 1), "ios.export_compliance"),
            ("quit-all", lambda t: t.replace('version_file = "whats_new_version.txt"\n', ''), "ios.whats_new.version_file"),
            ("verceltics", lambda t: t.replace('signing = "gradle-properties"', 'signing = "keystore-properties"'),
             "android.build.signing"),
            ("verceltics", lambda t: t.replace('pad_to_9_16 = false', 'pad_to_9_16 = true'), "only false"),
            ("quit-all", lambda t: t.replace('gate_policy = "after-prepare"', 'gate_policy = "sometimes"'), "ios.gate_policy"),
            ("scowld", lambda t: t.replace('on_existing = "upload-missing"', 'on_existing = "replace"'),
             "ios.github_release.on_existing"),
            ("quit-all", lambda t: t.replace('verify_ipa = true', 'verify_ipa = 1'), "ios.build.verify_ipa"),
            ("verceltics", lambda t: t.replace('with = { android-release = true }', 'with = { android-release = "${{ x }}" }'),
             "android.gate_jobs[0].with.android-release"),
        ]
        for app, edit, fragment in cases:
            with self.subTest(app=app, fragment=fragment), self.assertRaises(config.ConfigError) as caught:
                self.load(app, edit)
            self.assertIn(fragment, str(caught.exception))

    def test_configure_refuses_a_layout_the_flow_cannot_read(self) -> None:
        for app, edit, fragment in (
            ("verceltics", lambda t: t.replace("store/app-store/screenshots/iphone\"", "store/app-store/screenshots/phone\""),
             "folder named iphone"),
            ("abysm", lambda t: t.replace('groups = ["nonConsumables", "consumables", "subscriptions"]',
                                          'groups = ["subscriptions"]'), "catalog.groups"),
            ("scowld", lambda t: t + '\n[catalog]\npath = "x.json"\ngroups = ["tips"]\nid_prefix = "com.x."\n',
             "remove [catalog]"),
        ):
            with self.subTest(app=app), tempfile.TemporaryDirectory() as tmp:
                tree = trees.build_tree(app, Path(tmp))
                path = tree / "store" / "store-release.toml"
                path.write_text(edit(path.read_text()))
                cfg = config.load_for_app(tree)
                saved = support_apps.snapshot()
                try:
                    with self.assertRaises(config.ConfigError) as caught:
                        context.configure(cfg, tree)
                    self.assertIn(fragment, str(caught.exception))
                finally:
                    support_apps.restore(saved)


class FlowCliTests(unittest.TestCase):
    """run.py routes the release commands to the app's flow, with that app's arguments and switches."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.trees = {app: trees.build_tree(app, Path(cls.tmp.name) / app) for app in APPS}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_cli(self, app: str, *args: str, **env: str):
        return clirun.run(self.trees[app], *args, env=clirun.clean_env(**env))

    def test_gates_use_each_flows_own_rules(self) -> None:
        for app in APPS:
            cfg = config.load_for_app(self.trees[app])
            out = Path(self.tmp.name) / f"{app}.out"
            out.write_text("")
            with self.subTest(app=app):
                result = self.run_cli(app, "ios", "gates", GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF_NAME="main",
                                      GITHUB_OUTPUT=str(out))
                self.assertEqual(result.returncode, 0, result.stderr)
                values = clirun.outputs(out)
                self.assertEqual(values["mode"], "validate")
                self.assertEqual(values["build"], "true")
                result = self.run_cli(app, "ios", "gates", GITHUB_EVENT_NAME="push",
                                      GITHUB_REF_NAME=f"{cfg.ios.tag_prefix}0.0.1", GITHUB_OUTPUT=str(out))
                self.assertEqual(result.returncode, 1)
                result = self.run_cli(app, "ios", "gates", GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="main",
                                      IOS_RELEASE_AUTOMATION="maybe", GITHUB_OUTPUT=str(out))
                self.assertEqual(result.returncode, 1)

    def test_fud_ai_commands_are_refused_for_a_ported_flow(self) -> None:
        for args in (("listing", "prepare", "--platform", "ios", "--out", "x"), ("ios", "version-preflight", "--version", "1"),
                     ("whats-new", "prepare", "--out", "x")):
            with self.subTest(args=args):
                result = self.run_cli("scowld", *args)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_project_version_and_catalog(self) -> None:
        result = self.run_cli("abysm", "ios", "project-version", "--setting", "MARKETING_VERSION")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^[0-9]+(\.[0-9]+)*$")
        result = self.run_cli("scowld", "catalog", "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("has no product catalog", result.stdout)
        result = self.run_cli("pcm-notes", "catalog", "check", "--event", "workflow_dispatch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("on tag runs and pull requests", result.stdout)

    def test_check_inputs_skips_without_store_switches(self) -> None:
        for app in ("scowld", "pcm-notes", "quit-all"):
            with self.subTest(app=app):
                result = self.run_cli(app, "ios", "check-inputs", GATES_ASC="false")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("no App Store Connect switch is on", result.stdout)

    def test_the_archived_export_compliance_answer(self) -> None:
        good = Path(self.tmp.name) / "good.plist"
        bad = Path(self.tmp.name) / "bad.plist"
        good.write_bytes(plistlib.dumps({"ITSAppUsesNonExemptEncryption": False}))
        bad.write_bytes(plistlib.dumps({"ITSAppUsesNonExemptEncryption": "NO"}))
        for app in APPS:
            with self.subTest(app=app):
                result = self.run_cli(app, "ios", "export-compliance", "--info-plist", str(good))
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.run_cli(app, "ios", "export-compliance", "--info-plist", str(bad))
                self.assertEqual(result.returncode, 1)

    def test_store_step_refuses_fud_ai_directories(self) -> None:
        result = self.run_cli("abysm", "ios", "store-step", "--metadata-dir", "x", "--version", "1.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("takes no --metadata-dir", result.stderr)

    def test_verceltics_android_commands(self) -> None:
        out = Path(self.tmp.name) / "android.out"
        out.write_text("")
        result = self.run_cli("verceltics", "android", "gates", GITHUB_EVENT_NAME="workflow_dispatch",
                              GITHUB_REF_NAME="main", GITHUB_OUTPUT=str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(clirun.outputs(out)["mode"], "validate")
        result = self.run_cli("verceltics", "android", "play-listing", "whats-new", "--tag", "android-v0.0.0")
        self.assertNotEqual(result.returncode, 0)


def fake_publisher(*, bundles=(), tracks=None):
    """A Play androidpublisher service the way the app's PlayReadTests faked it."""
    service = mock.MagicMock()
    edits = service.edits.return_value
    edits.insert.return_value.execute.return_value = {"id": "EDIT"}
    edits.bundles.return_value.list.return_value.execute.return_value = {
        "bundles": [{"versionCode": code} for code in bundles]}
    edits.apks.return_value.list.return_value.execute.return_value = {}
    edits.tracks.return_value.list.return_value.execute.return_value = {
        "tracks": [{"track": name, **track} for name, track in (tracks or {}).items()]}
    edits.tracks.return_value.get.side_effect = lambda **kw: mock.Mock(
        execute=mock.Mock(return_value=(tracks or {}).get(kw["track"], {})))
    return service, edits


class VercelticsPlayTests(unittest.TestCase):
    """check-version-code and play-state through a fake publisher (the app's PlayReadTests case, which
    swapped sys.modules['play_listing'], a name the ported package does not import it by)."""

    def run_command(self, release, listing, service, *args):
        out = io.StringIO()
        with tempfile.NamedTemporaryFile("r", suffix=".out") as outputs, \
                mock.patch.object(listing, "build_android_publisher", return_value=service), \
                mock.patch.dict(os.environ, {"PLAY_SERVICE_ACCOUNT_JSON": "{}", "GITHUB_OUTPUT": outputs.name}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                release.main(list(args))
                code = 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            written = Path(outputs.name).read_text()
        return code, out.getvalue(), written

    def test_commands_read_through_a_deleted_throwaway_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = trees.build_tree("verceltics", Path(tmp))
            cfg = config.load_for_app(tree)
            saved = support_apps.snapshot()
            try:
                context.configure(cfg, tree)
                release = commands.module("verceltics", "android_release")
                listing = commands.module("verceltics", "play_listing")
                _, code = release.gradle_version(release.GRADLE.read_text(encoding="utf-8"))
                service, edits = fake_publisher(bundles=[code - 1])
                status, output, _ = self.run_command(release, listing, service, "check-version-code")
                self.assertEqual(status, 0, output)
                edits.delete.assert_called()
                service, _ = fake_publisher(bundles=[code])
                status, output, _ = self.run_command(release, listing, service, "check-version-code")
                self.assertEqual(status, 1)
                self.assertIn("already uploaded", output)
                service, _ = fake_publisher(bundles=[code - 1])
                status, output, written = self.run_command(release, listing, service, "play-state", "--tracks", "internal")
                self.assertEqual(status, 0, output)
                self.assertIn("uploaded=false", written)
            finally:
                support_apps.restore(saved)


class GatePolicyTests(unittest.TestCase):
    def test_when_uploading_accepts_skipped_gates_only_without_an_upload(self) -> None:
        self.assertEqual(stage.gate_check("tests=skipped", ["tests"], release=True, skipped_ok=True),
                         {"tests": "skipped"})
        with self.assertRaises(stage.StageError):
            stage.gate_check("tests=success", ["tests"], release=True, skipped_ok=True)
        with self.assertRaises(stage.StageError):
            stage.gate_check("tests=skipped", ["tests"], release=True)
        self.assertEqual(stage.gate_check("tests=success", ["tests"], release=True), {"tests": "success"})


class GithubReleaseTests(unittest.TestCase):
    def cfg(self, app: str) -> config.Config:
        return config.load(TOOL_ROOT / "examples" / app / "store-release.toml")

    def test_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for app, uploaded, expected in (
                ("abysm", "7", "ABYSM v1.0.1 (build 7)"), ("abysm", "", "ABYSM v1.0.1"),
                ("pcm-notes", "", "PCM Notes v1.0.1"), ("scowld", "", "Scowld 1.0.1"),
            ):
                with self.subTest(app=app, uploaded=uploaded):
                    self.assertEqual(github.ios_title(self.cfg(app), root, version="1.0.1", uploaded_build=uploaded,
                                                      tag="v1.0.1"), expected)
            pbxproj = root / "ios" / "Quit All.xcodeproj" / "project.pbxproj"
            pbxproj.parent.mkdir(parents=True)
            pbxproj.write_text("\t\t\t\tCURRENT_PROJECT_VERSION = 12;\n")
            self.assertEqual(github.ios_title(self.cfg("quit-all"), root, version="1.2.3", uploaded_build="", tag="v1.2.3"),
                             "Quit All v1.2.3 (build 12)")

    def commands(self, app: str, *, exists: bool, attached=(), assets=()) -> list[list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            pbxproj = Path(tmp) / self.cfg(app).ios.pbxproj
            pbxproj.parent.mkdir(parents=True)
            pbxproj.write_text("CURRENT_PROJECT_VERSION = 3;\n")
            return github.ios_release_commands(self.cfg(app), Path(tmp), tag="v1.0", version="1.0", uploaded_build="",
                                               notes_file="", repo="o/r", tags=["v9.0"], exists=exists,
                                               assets=list(assets), attached=list(attached))

    def test_existing_release_policies(self) -> None:
        self.assertEqual(self.commands("abysm", exists=True), [])
        self.assertEqual(self.commands("pcm-notes", exists=True), [])
        created = self.commands("quit-all", exists=True)
        self.assertEqual(created[0][:3], ["gh", "release", "create"])
        assets = ["/d/scowld-1.0-source.tar.gz", "/d/scowld-1.0-source.tar.gz.sigstore.json",
                  "/d/scowld-1.0-source.tar.gz.sha256"]
        self.assertEqual(self.commands("scowld", exists=True, assets=assets,
                                       attached=["scowld-1.0-source.tar.gz", "scowld-1.0-source.tar.gz.sha256"]),
                         [["gh", "release", "upload", "v1.0", "--repo", "o/r", *assets, "--clobber"]])
        self.assertEqual(self.commands("scowld", exists=True, assets=assets, attached=[Path(a).name for a in assets]), [])

    def test_always_latest_even_below_a_higher_tag(self) -> None:
        for app in ("scowld", "verceltics", "abysm", "pcm-notes", "quit-all"):
            with self.subTest(app=app):
                created = self.commands(app, exists=False)
                self.assertIn("--latest", created[0])

    def test_source_archive_assets_must_all_exist(self) -> None:
        cfg = self.cfg("scowld")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(github.GithubError):
                github.source_archive_assets(cfg, "1.0", tmp)
            for suffix in github.SOURCE_ARCHIVE_SUFFIXES:
                (Path(tmp) / f"scowld-1.0-source.tar.gz{suffix}").write_text("x")
            self.assertEqual(len(github.source_archive_assets(cfg, "1.0", tmp)), 3)
        self.assertEqual(github.source_archive_assets(self.cfg("abysm"), "1.0", ""), [])

    def test_verceltics_android_edits_then_uploads_and_is_latest_in_production(self) -> None:
        cfg = self.cfg("verceltics")
        with tempfile.TemporaryDirectory() as tmp:
            gradle = Path(tmp) / cfg.android.gradle_file
            gradle.parent.mkdir(parents=True)
            gradle.write_text('versionCode = 4\nversionName = "1.0"\n')
            commands_ = github.android_release_commands(cfg, Path(tmp), tag="android-v1.0", production=True,
                                                        assets_dir=Path(tmp), notes_file="", repo="o/r",
                                                        tags=["android-v2.0"], exists=True)
        self.assertEqual([c[2] for c in commands_], ["edit", "upload"])
        self.assertIn("--latest", commands_[0])
        self.assertIn("verceltics-android-1.0-4.aab", commands_[1][-4])


class ReleaseNotesRulesTests(unittest.TestCase):
    """The apps' release-notes rules hold in store-release's release-notes (ABYSM's, PCM Notes' and
    Quit All's own cases): the tag's section, a missing file or section means generated notes, an
    empty or duplicated section fails, and the unique_headings check catches any repeated heading."""

    def test_rules(self) -> None:
        text = "# Notes\n\n## v1.0.1\n\n- One\n\n## v1.0.0\n\n- Zero\n"
        self.assertEqual(release_notes.section_for_tag("v1.0.1", text), "- One")
        self.assertIsNone(release_notes.section_for_tag("v2.0", text))
        with self.assertRaises(ValueError):
            release_notes.section_for_tag("v1.0.1", "## v1.0.1\n\n")
        with self.assertRaises(ValueError):
            release_notes.section_for_tag("v1.0.1", "## v1.0.1\n- a\n## v1.0.1\n- b\n")
        self.assertEqual(release_notes.duplicate_headings("## v1.0.1\n- a\n## v1.0.1\n- b\n## v1\n"), ["v1.0.1"])


class RedactUnionTests(unittest.TestCase):
    """Every app's redaction rules: any case, UTF-8 and UTF-16 either way, symlinks removed, and IDs
    in file or folder names renamed (Quit All's and Verceltics' rule)."""

    def test_names_and_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "logs"
            (folder / "KEYID123-dir").mkdir(parents=True)
            (folder / "KEYID123-dir" / "keyid123.log").write_text("key KEYID123 issuer ISSUER-9\n")
            (folder / "utf16.txt").write_bytes("id keyid123".encode("utf-16-be"))
            (folder / "link").symlink_to(folder / "utf16.txt")
            changed, removed = redact.redact_folder(folder, {"ASC_KEY_ID": "KEYID123", "ASC_ISSUER_ID": "ISSUER-9"})
            names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))
            self.assertFalse(any("keyid123" in n.lower() for n in names), names)
            texts = [p.read_bytes() for p in folder.rglob("*") if p.is_file()]
            self.assertFalse(any(b"keyid123" in t.lower() for t in texts))
            self.assertTrue(all(b"ISSUER-9" not in t for t in texts))
            self.assertFalse((folder / "link").exists())


class FlowPathsTests(unittest.TestCase):
    def test_flow_jobs_check_out_what_the_flow_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from store_release import paths  # noqa: PLC0415

            env = {"GITHUB_WORKFLOW_REF": "o/r/.github/workflows/ios-release.yml@refs/tags/v1", "GITHUB_REPOSITORY": "o/r"}
            for app in APPS:
                cfg = config.load_for_app(trees.build_tree(app, Path(tmp) / app))
                with self.subTest(app=app):
                    prepare = paths.patterns(cfg, "ios-prepare", config_path="store/store-release.toml", environ=env)
                    self.assertIn("/store/", prepare)
                    self.assertNotIn("/None", prepare)
                    if cfg.catalog is not None and cfg.catalog.storekit:
                        self.assertIn(f"/{cfg.catalog.storekit}", prepare)
                    build = paths.patterns(cfg, "ios-build", config_path="store/store-release.toml", environ=env)
                    for hook in cfg.ios.build.post_archive:
                        self.assertIn(f"/{hook}", build)
                    self.assertEqual(paths.patterns(cfg, "ios-source-archive", config_path="store/store-release.toml",
                                                    environ=env), ["/store/store-release.toml"])


if __name__ == "__main__":
    unittest.main()
