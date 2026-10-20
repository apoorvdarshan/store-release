"""The ported flows' read-only store plan (store_release.flows.plan), against a fake App Store Connect.

When the App Store version does not exist yet and IOS_CREATE_VERSION would create it, the flow's
preflight cannot run (it needs the version). The plan then runs every check that does not need
it, as a tag run would make them, and lists what only the created version can show.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from store_release import config, context
from store_release.flows import commands
from store_release.flows import plan as flow_plan
from support import apps as support_apps

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOL_ROOT / "tests"))
from flows import trees  # noqa: E402

APPS = ("scowld", "verceltics", "abysm", "pcm-notes", "quit-all")
VERSION = "9.9.9"  # trees.build_tree writes a release notes section for <prefix>9.9.9
LIVE_WHATS_NEW = "Bug fixes from the last release."
SWITCHES = {"IOS_RELEASE_AUTOMATION": "true", "IOS_CREATE_VERSION": "true", "IOS_UPLOAD_BUILD": "true",
            "IOS_UPLOAD_WHATS_NEW": "true", "IOS_SUBMIT_REVIEW": "true"}


class FakeAsc:
    """GET-only App Store Connect: one app with a live 9.9.8 and, unless missing, the version under plan."""

    def __init__(self, app_id: str, *, planned: str | None = None, live_whats_new: str = LIVE_WHATS_NEW,
                 drafts: int = 0, iaps: list[dict] | None = None, subscriptions: list[dict] | None = None) -> None:
        self.app_id = app_id
        self.versions = [{"id": "V-LIVE", "type": "appStoreVersions",
                          "attributes": {"versionString": "9.9.8", "platform": "IOS",
                                         "appStoreState": "READY_FOR_SALE", "createdDate": "2026-01-01T00:00:00Z"}}]
        if planned:
            self.versions.append({"id": "V-NEW", "type": "appStoreVersions",
                                  "attributes": {"versionString": planned, "platform": "IOS",
                                                 "appStoreState": "PREPARE_FOR_SUBMISSION",
                                                 "createdDate": "2026-02-01T00:00:00Z"}})
        self.live_whats_new = live_whats_new
        self.drafts = [{"id": f"SUB{i}", "type": "reviewSubmissions", "attributes": {"state": "READY_FOR_REVIEW"}}
                       for i in range(drafts)]
        self.iaps = iaps or []
        self.subscriptions = subscriptions or []
        self.paths: list[str] = []

    def collection(self, rows: list[dict]) -> dict:
        return {"data": rows, "links": {}}

    def get(self, path: str) -> dict:
        self.paths.append(path)
        path = path.removeprefix("https://api.appstoreconnect.apple.com").removeprefix("/v1").removeprefix("/v2")
        base, _, query = path.partition("?")
        params = urllib.parse.parse_qs(query)
        if base == "/apps":
            return self.collection([{"id": self.app_id, "type": "apps",
                                     "attributes": {"bundleId": params.get("filter[bundleId]", [""])[0]}}])
        if base == f"/apps/{self.app_id}/appStoreVersions":
            wanted = params.get("filter[versionString]")
            rows = [row for row in self.versions if not wanted or row["attributes"]["versionString"] in wanted]
            return self.collection(rows)
        if base.startswith("/appStoreVersions/") and base.endswith("/appStoreVersionLocalizations"):
            version_id = base.split("/")[2]
            text = self.live_whats_new if version_id == "V-LIVE" else ""
            return self.collection([{"id": f"L-{version_id}", "type": "appStoreVersionLocalizations",
                                     "attributes": {"locale": "en-US", "whatsNew": text}}])
        if base == f"/apps/{self.app_id}/reviewSubmissions":
            return self.collection(self.drafts)
        if base.startswith("/reviewSubmissions/") and base.endswith("/items"):
            return self.collection([])
        if base == f"/apps/{self.app_id}/subscriptionGroups":
            return self.collection([{"id": "G1", "type": "subscriptionGroups"}] if self.subscriptions else [])
        if base == "/subscriptionGroups/G1/subscriptions":
            return self.collection(self.subscriptions)
        if base == f"/apps/{self.app_id}/inAppPurchasesV2":
            return self.collection(self.iaps)
        if base.startswith(("/subscriptions/", "/inAppPurchases/", "/inAppPurchasesV2/")):
            return self.collection([])
        raise AssertionError(f"the fake App Store Connect has no {path}")

    def request(self, method: str, path: str, *args, **kwargs):
        if method.upper() != "GET":
            raise AssertionError(f"a write reached the fake: {method} {path}")
        return self.get(path)

    def post(self, path, body):  # pragma: no cover - the plan's client refuses first
        raise AssertionError(f"a write reached the fake: POST {path}")

    def patch(self, path, body):  # pragma: no cover
        raise AssertionError(f"a write reached the fake: PATCH {path}")


class FlowPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        saved = support_apps.snapshot()
        self.addCleanup(support_apps.restore, saved)

    def tree(self, app: str, *, whats_new: str = "New in this release: faster sync.") -> Path:
        self.count = getattr(self, "count", 0) + 1
        root = trees.build_tree(app, self.tmp / f"{app}-{self.count}").resolve()
        cfg = config.load_for_app(root)
        if cfg.ios.whats_new is not None and cfg.ios.whats_new.source == "files":
            listing = root / (cfg.ios.listing.dir if cfg.ios.listing else "store/metadata/ios")
            (listing / "en-US" / "whats_new.txt").write_text(whats_new + "\n", encoding="utf-8")
            if cfg.ios.whats_new.version_file:
                (listing / cfg.ios.whats_new.version_file).write_text(VERSION + "\n", encoding="utf-8")
        return root

    def plan(self, app: str, root: Path, fake: FakeAsc | None = None, *, switches: dict | None = None,
             catalog_ok: bool = True) -> tuple[int, str, dict, FakeAsc]:
        cfg = config.load_for_app(root)
        context.configure(cfg, root)
        fake = fake or FakeAsc(cfg.ios.asc_app_id)
        asc = commands.module(cfg.ios.flow, "asc_release")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("IOS_", "UPLOAD_", "SUBMIT_", "GITHUB_"))}
        env.update(SWITCHES if switches is None else switches)
        work = root.parent / f"{root.name}-plan"
        out = io.StringIO()
        catalog = mock.patch.object(commands, "catalog_check", return_value=0) if catalog_ok else contextlib.nullcontext()
        with mock.patch.object(asc, "client_from_environment", lambda *a, **k: fake), catalog, \
                mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            code = flow_plan.ios_plan(mock.Mock(config=cfg), version=VERSION, work_dir=work, environ=env)
        result = json.loads((work / "store-plan.json").read_text())
        return code, out.getvalue(), result, fake

    def assert_planned_version_checks(self, app: str, output: str, result: dict) -> None:
        self.assertIn(f"App Store version {VERSION} is missing: IOS_CREATE_VERSION would create it first",
                      result["lines"])
        note = next(line for line in result["lines"] if line.startswith("not checked until the tag run creates"))
        for item in ("the version's state", "its What's New localizations", "the version's place in the review draft"):
            self.assertIn(item, note)
        self.assertIn("::notice title=store-plan::not checked until the tag run creates", output)
        self.assertIn("== the app's draft review submissions: passed", output)

    def test_a_missing_version_still_gets_every_check_that_needs_no_version(self) -> None:
        for app in APPS:
            with self.subTest(app=app):
                code, output, result, fake = self.plan(app, self.tree(app))
                self.assertEqual(code, 0, output)
                self.assertEqual(result["problems"], [], output)
                self.assertEqual(result["status"], "store-plan: CLEAN")
                self.assert_planned_version_checks(app, output, result)
                self.assertIn("== the local files the enabled switches upload (offline): passed", output)
                self.assertIn("every check that needs no App Store version passed", result["lines"])
                self.assertFalse([path for path in fake.paths if "V-NEW" in path])

    def test_a_stale_whats_new_is_a_problem_before_the_version_exists(self) -> None:
        # The flows that refuse the live version's What's New: Scowld (preflight and store step) and
        # PCM Notes (store step).
        for app in ("scowld", "pcm-notes"):
            with self.subTest(app=app):
                code, output, result, _ = self.plan(app, self.tree(app, whats_new=LIVE_WHATS_NEW))
                self.assertEqual(code, 0, output)
                self.assertEqual(result["status"], "store-plan: 1 PROBLEM(S)")
                self.assertRegex(result["problems"][0], r"What's New|whats_new.txt")
                self.assertIn("9.9.8", result["problems"][0])
                self.assertIn("== What's New is not the live version's (every locale): a tag run would fail here",
                              output)

    def test_quit_alls_whats_new_version_file_is_checked_with_the_local_files(self) -> None:
        root = self.tree("quit-all")
        (root / "store/metadata/ios/whats_new_version.txt").write_text("9.9.8\n", encoding="utf-8")
        code, output, result, _ = self.plan("quit-all", root)
        self.assertEqual(result["status"], "store-plan: 1 PROBLEM(S)", output)
        self.assertIn("whats_new_version.txt", result["problems"][0])

    def test_verceltics_whats_new_needs_the_tags_release_notes(self) -> None:
        root = self.tree("verceltics")
        (root / "RELEASE_NOTES.md").write_text("# Release notes\n\n## ios-v9.9.8\n\n- Older.\n", encoding="utf-8")
        code, output, result, _ = self.plan("verceltics", root)
        self.assertEqual(result["status"], "store-plan: 1 PROBLEM(S)", output)
        self.assertIn("== the local files the enabled switches upload (offline): a tag run would fail here", output)

    def test_products_and_drafts_that_would_stop_review(self) -> None:
        pending = [{"id": "IAP1", "type": "inAppPurchases",
                    "attributes": {"productId": "com.example.tip", "name": "Tip", "state": "READY_TO_SUBMIT"}}]
        root = self.tree("scowld")
        cfg = config.load_for_app(root)
        code, output, result, _ = self.plan("scowld", root, FakeAsc(cfg.ios.asc_app_id, iaps=pending))
        self.assertEqual(result["status"], "store-plan: 1 PROBLEM(S)", output)
        self.assertIn("in-app products that would have to go to review", result["problems"][0])
        code, output, result, _ = self.plan("scowld", root, FakeAsc(cfg.ios.asc_app_id, drafts=2))
        self.assertEqual(result["status"], "store-plan: 1 PROBLEM(S)", output)
        self.assertIn("ambiguous: 2 draft review submissions", result["problems"][0])
        # ABYSM: a catalog product App Store Connect has no version of would fail its product step.
        root = self.tree("abysm")
        cfg = config.load_for_app(root)
        products = json.loads((root / cfg.catalog.path).read_text())
        product_id = next(item["id"] for group in cfg.catalog.groups for item in products.get(group) or [])
        iap = [{"id": "IAP9", "type": "inAppPurchases",
                "attributes": {"productId": product_id, "name": "Unlock", "state": "READY_TO_SUBMIT"}}]
        code, output, result, _ = self.plan("abysm", root, FakeAsc(cfg.ios.asc_app_id, iaps=iap))
        self.assertEqual(result["status"], "store-plan: 1 PROBLEM(S)", output)
        self.assertIn(f"({product_id}): no version to submit", result["problems"][0])

    def test_the_products_a_store_step_attaches_are_listed_as_not_checked(self) -> None:
        for app in ("verceltics", "pcm-notes", "quit-all"):
            with self.subTest(app=app):
                _, output, result, _ = self.plan(app, self.tree(app))
                note = next(line for line in result["lines"] if line.startswith("not checked until"))
                self.assertIn("the catalog products that go to review with it", note)
        _, _, result, _ = self.plan("scowld", self.tree("scowld"))
        note = next(line for line in result["lines"] if line.startswith("not checked until"))
        self.assertNotIn("catalog products", note)

    def test_the_catalog_check_a_tag_run_makes_is_planned(self) -> None:
        # The tree has no StoreKit file or product source, so PCM Notes' tag-run catalog check fails.
        code, output, result, _ = self.plan("pcm-notes", self.tree("pcm-notes"), catalog_ok=False)
        self.assertIn("== the catalog check a tag run's prepare makes (offline): a tag run would fail here", output)
        self.assertGreaterEqual(len(result["problems"]), 1)

    def test_without_ios_create_version_a_missing_version_is_the_problem(self) -> None:
        switches = {**SWITCHES, "IOS_CREATE_VERSION": "false"}
        _, output, result, _ = self.plan("scowld", self.tree("scowld"), switches=switches)
        self.assertEqual(result["problems"], [f"App Store version {VERSION} does not exist and IOS_CREATE_VERSION is off"])

    def test_only_create_version_on_checks_no_store_step(self) -> None:
        switches = {"IOS_RELEASE_AUTOMATION": "true", "IOS_CREATE_VERSION": "true"}
        _, output, result, _ = self.plan("abysm", self.tree("abysm"), switches=switches)
        self.assertEqual(result["status"], "store-plan: CLEAN", output)
        self.assertFalse([line for line in result["lines"] if line.startswith("not checked until")])

    def test_an_existing_version_gets_the_offline_checks_and_the_preflight(self) -> None:
        for app in APPS:
            with self.subTest(app=app):
                root = self.tree(app)
                cfg = config.load_for_app(root)
                with mock.patch.object(commands, "preflight", return_value=0) as preflight:
                    _, output, result, _ = self.plan(app, root, FakeAsc(cfg.ios.asc_app_id, planned=VERSION))
                self.assertEqual(result["status"], "store-plan: CLEAN", output)
                preflight.assert_called_once_with(cfg, app, ["--version", VERSION])
                self.assertIn(f"App Store version {VERSION} exists", result["lines"])
                self.assertIn("the preflight passed: the version can take every enabled operation", result["lines"])
                self.assertIn("== the local files the enabled switches upload (offline): passed", output)
                self.assertFalse([line for line in result["lines"] if line.startswith("not checked until")])

    def test_an_existing_pcm_notes_version_gets_the_store_steps_whats_new_check(self) -> None:
        root = self.tree("pcm-notes", whats_new=LIVE_WHATS_NEW)
        cfg = config.load_for_app(root)
        fake = FakeAsc(cfg.ios.asc_app_id, planned=VERSION)
        with mock.patch.object(commands, "preflight", return_value=0):
            _, output, result, _ = self.plan("pcm-notes", root, fake)
        self.assertIn(f"App Store version {VERSION} exists", result["lines"])
        self.assertEqual(len(result["problems"]), 1, output)
        self.assertIn("still holds the What's New of live version 9.9.8", result["problems"][0])
        with mock.patch.object(commands, "preflight", return_value=0):
            _, output, result, _ = self.plan("pcm-notes", self.tree("pcm-notes"), FakeAsc(cfg.ios.asc_app_id,
                                                                                           planned=VERSION))
        self.assertEqual(result["status"], "store-plan: CLEAN", output)


if __name__ == "__main__":
    unittest.main()
