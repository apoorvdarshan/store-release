"""Parity cases 6, 8, 9 and 10: export compliance, the store step, version creation and preflight,
and the build wait, on identical inputs and identically seeded fakes."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import sys
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from store_release.ios import asc
from store_release.ios import release as ios_release
from support import apps as support_apps
from support.upstream import ParityCase, outcome

support_apps.configure_fud_ai_defaults()

from support.asc_fakes import (  # noqa: E402 - needs the configured modules
    APP, STARTED, VERSION, CreativeFake, FakeAsc, FakeClock, PreflightClient, SequenceClient, VersionCreationClient,
    asc_version, build_row, creative_png, image_item, make_png, pre_row, version_item,
)

FLAGS = {"UPLOAD_LISTING", "UPLOAD_WHATS_NEW", "UPLOAD_SCREENSHOTS", "UPLOAD_CREATIVE_ASSETS", "SUBMIT_IOS_REVIEW",
         "PUBLIC_RELEASE", "CREATE_IOS_VERSION"}


def canonical(body: Any) -> str | None:
    return None if body is None else json.dumps(body, sort_keys=True)


class Recording:
    """Wraps a fake App Store Connect client and logs (method, path, body) of every call."""

    def __init__(self, fake: Any) -> None:
        self.fake = fake
        self.log: list[tuple] = []

    def get(self, path: str) -> dict:
        self.log.append(("GET", path, None))
        return self.fake.get(path)

    def post(self, path: str, body: dict) -> dict:
        self.log.append(("POST", path, canonical(body)))
        return self.fake.post(path, body)

    def patch(self, path: str, body: dict) -> dict:
        self.log.append(("PATCH", path, canonical(body)))
        return self.fake.patch(path, body)

    def request(self, method: str, path: str, body: dict | None = None):
        self.log.append((method, path, canonical(body)))
        return self.fake.request(method, path, body)

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.log.append(("UPLOAD", url, hashlib.sha256(data).hexdigest()))
        upload = getattr(self.fake, "upload_bytes", None)
        if upload is not None:
            upload(url, data, headers, method=method)


class StoreStepParityTests(ParityCase):
    PNG = make_png(4, 4)
    IAP = {"id": "IAP1", "attributes": {"name": "Credits 50", "productId": "com.apoorvdarshan.calorietracker.credits.50",
                                        "state": "READY_TO_SUBMIT"}}

    def setUp(self) -> None:
        super().setUp()
        loc = self.tmp / "meta" / "ios" / "en-US"
        loc.mkdir(parents=True)
        (loc / "description.txt").write_text("Track food with AI\n")
        (loc / "whats_new.txt").write_text("Fixes.\n")
        self.shots = self.tmp / "shots"
        for folder, _, _ in asc.SCREENSHOT_SETS:
            (self.shots / folder).mkdir(parents=True)
            (self.shots / folder / "01-home.png").write_bytes(self.PNG)
        self.watch_png = make_png(416, 496)
        self.creative = self.tmp / "creative"
        self.header = self.creative / "header" / "header.png"
        self.search = self.creative / "search-results" / "search.png"
        for path, raw in ((self.header, creative_png(3840, 1646)), (self.search, creative_png(1920, 1280))):
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
        self.same_text = {"description": "Track food with AI", "whatsNew": "Fixes."}
        self.same_shots = [{"fileName": "01-home.png", "fileSize": len(self.PNG),
                            "sourceFileChecksum": hashlib.md5(self.PNG).hexdigest()}]

    def image(self, image_id: str, path: Path, state: str, created: str = "2026-10-20") -> dict:
        return {"id": image_id, "attributes": {"fileName": path.name, "fileSize": path.stat().st_size, "state": state,
                                               "createdDate": created, "imageAsset": {"width": 1, "height": 1}}}

    def shot(self, name: str, raw: bytes) -> dict:
        return {"fileName": name, "fileSize": len(raw), "sourceFileChecksum": hashlib.md5(raw).hexdigest()}

    def run_step(self, module, factory: Callable[[], Any], env: dict[str, str], argv: list[str],
            patches: Callable[[Any, Recording], list] = lambda module, recording: []) -> tuple:
        recording = Recording(factory())
        full_env = {k: v for k, v in os.environ.items() if k not in FLAGS}
        full_env.update(env)
        context = [mock.patch.dict(os.environ, full_env, clear=True),
                   mock.patch.object(sys, "argv", ["asc_release.py", *argv]),
                   mock.patch.object(module, "client_from_environment", return_value=recording),
                   mock.patch.object(module.time, "sleep"), *patches(module, recording)]
        for item in context:
            item.start()
        try:
            result = outcome(module.main)
        finally:
            for item in reversed(context):
                item.stop()
        return result, recording.log

    def compare(self, factory, env, argv, patches=lambda module, recording: []) -> tuple:
        upstream = self.run_step(self.up.asc_release, factory, env, argv, patches)
        package = self.run_step(asc, factory, env, argv, patches)
        self.assertEqual(package[0], upstream[0])
        self.assertEqual(package[1], upstream[1])
        return upstream

    def build(self) -> list[str]:
        return ["--version", VERSION, "--build-id", "B9", "--build-number", "40"]

    def paths(self) -> list[str]:
        return ["--metadata-dir", str(self.tmp / "meta"), "--screenshots-dir", str(self.shots),
                "--creative-dir", str(self.creative)]

    def record_uploads(self, module, recording):
        return [mock.patch.object(module, "upload_screenshot_file",
                                  lambda client, set_id, path: recording.log.append(("SHOT", set_id, path.name)))]

    def test_submit_and_rerun_scenarios(self) -> None:
        listing = {"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true"}
        submit = {"SUBMIT_IOS_REVIEW": "true"}
        cases = {
            "fresh submit": (FakeAsc, submit),
            "public release": (FakeAsc, {**submit, "PUBLIC_RELEASE": "true"}),
            "selection reverts": (lambda: FakeAsc(revert_after_select=True), submit),
            "waiting, another build": (lambda: FakeAsc(state="WAITING_FOR_REVIEW", attached="OTHER"), submit),
            "waiting, this build": (lambda: FakeAsc(state="WAITING_FOR_REVIEW", attached="B9"), submit),
            "ready for review rerun": (lambda: FakeAsc(state="READY_FOR_REVIEW",
                                                       live_text={"description": "Old", "whatsNew": "Fixes."}),
                                       {**listing, **submit}),
            "waiting, same text": (lambda: FakeAsc(state="WAITING_FOR_REVIEW", attached="B9",
                                                   live_text=self.same_text), {**listing, **submit}),
            "waiting, changed text": (lambda: FakeAsc(state="WAITING_FOR_REVIEW", attached="B9",
                                                      live_text={**self.same_text, "description": "Older"}), listing),
            "locked screenshots, same": (lambda: FakeAsc(state="READY_FOR_REVIEW", live_shots=self.same_shots),
                                         {"UPLOAD_SCREENSHOTS": "true", **submit}),
            "locked screenshots, changed": (lambda: FakeAsc(state="READY_FOR_REVIEW", live_text={"description": "Old"},
                                                            live_shots=[{**self.same_shots[0],
                                                                         "sourceFileChecksum": "0" * 32}]),
                                            {"UPLOAD_SCREENSHOTS": "true", **listing, **submit}),
            "editable, all match": (lambda: FakeAsc(live_text=self.same_text, live_shots=self.same_shots),
                                    {**listing, "UPLOAD_SCREENSHOTS": "true"}),
            "editable, upload every set": (FakeAsc, {"UPLOAD_SCREENSHOTS": "true"}),
            "in review refuses text": (lambda: FakeAsc(state="IN_REVIEW", live_text={"description": "Old"}), listing),
            "ready for sale refuses text": (lambda: FakeAsc(state="READY_FOR_SALE", live_text={"description": "Old"}),
                                            listing),
            "iap after the recheck": (lambda: FakeAsc(iaps=[self.IAP]), submit),
            "iap, failed recheck": (lambda: FakeAsc(iaps=[self.IAP], revert_after_select=True), submit),
            "nothing on": (FakeAsc, {}),
            "public release without submit": (FakeAsc, {"UPLOAD_LISTING": "true", "PUBLIC_RELEASE": "true"}),
        }
        for label, (factory, env) in cases.items():
            with self.subTest(label=label):
                self.compare(factory, env, self.build() + self.paths(), self.record_uploads)

    def test_a_set_without_screenshots_fails_before_any_call(self) -> None:
        (self.shots / "ipad-13" / "01-home.png").unlink()
        result = self.compare(FakeAsc, {"UPLOAD_SCREENSHOTS": "true"}, self.build() + self.paths())
        self.assertEqual(result[0][:2], ("exit", 1))
        self.assertEqual(result[1], [])

    def test_watch_scenarios(self) -> None:
        watch = self.shots / asc.WATCH_FOLDER
        watch.mkdir()
        (watch / "01-home-glance.png").write_bytes(self.watch_png)
        (watch / "02-ultra-3.png").write_bytes(make_png(422, 514))
        live = {display: [self.shot("01-home.png", self.PNG)] for _, display, _ in asc.SCREENSHOT_SETS}
        same = {**live, "APP_WATCH_SERIES_10": [self.shot("01-home-glance.png", self.watch_png)],
                "APP_WATCH_ULTRA": [self.shot("02-ultra-3.png", make_png(422, 514))]}
        env = {"UPLOAD_SCREENSHOTS": "true"}
        cases = {
            "upload watch sets": (FakeAsc, env),
            "watch sets match": (lambda: FakeAsc(live_by_display=same), env),
            "a watch set differs": (lambda: FakeAsc(live_by_display={**same, "APP_WATCH_SERIES_10": [
                self.shot("01-old.png", b"old")]}), env),
            "locked, watch differs": (lambda: FakeAsc(state="READY_FOR_REVIEW", live_by_display=live),
                                      {**env, "SUBMIT_IOS_REVIEW": "true"}),
        }
        for label, (factory, case_env) in cases.items():
            with self.subTest(label=label):
                self.compare(factory, case_env, self.build() + self.paths(), self.record_uploads)
        (watch / "03-phone.png").write_bytes(make_png(1290, 2796))
        with self.subTest(label="not a watch size"):
            self.compare(FakeAsc, env, self.build() + self.paths(), self.record_uploads)

    def test_creative_scenarios(self) -> None:
        env = {"UPLOAD_CREATIVE_ASSETS": "true"}
        submit = {**env, "SUBMIT_IOS_REVIEW": "true"}
        header, search = self.header, self.search
        cases = {
            "new images": (lambda: CreativeFake(), env, []),
            "approved and placed": (lambda: CreativeFake(
                library=[self.image("H", header, "APPROVED"), self.image("S", search, "APPROVED")],
                placements={"PL-H": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "LOC1"},
                            "PL-S": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "S", "loc": "LOC1"}}), env, []),
            "approved, placed elsewhere": (lambda: CreativeFake(
                library=[self.image("H", header, "APPROVED"), self.image("S", search, "APPROVED")],
                placements={"PL-OLD": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "LOC-712"}}), env, []),
            "swap out": (lambda: CreativeFake(
                library=[self.image("H", header, "APPROVED"), self.image("S", search, "IN_REVIEW")],
                placements={"PL-OLD": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "OLDIMG", "loc": "LOC1"}}), env, []),
            "interrupted upload": (lambda: CreativeFake(
                library=[self.image("STALE", header, "AWAITING_UPLOAD", "2026-10-21"),
                         self.image("S", search, "APPROVED")]), env, []),
            "still processing": (lambda: CreativeFake(
                library=[self.image("H", header, "UPLOAD_COMPLETE"), self.image("S", search, "APPROVED")],
                processing={"H": ["UPLOAD_COMPLETE", "COMPLETE", "PREPARE_FOR_SUBMISSION"]}), env, []),
            "processing failed": (lambda: CreativeFake(library=[self.image("H", header, "UPLOAD_COMPLETE")],
                                                       processing={"H": ["FAILED"]}), env, []),
            "in review": (lambda: CreativeFake(library=[self.image("H", header, "IN_REVIEW"),
                                                        self.image("S", search, "WAITING_FOR_REVIEW")]), env, []),
            "rejected": (lambda: CreativeFake(library=[self.image("H", header, "REJECTED"),
                                                       self.image("S", search, "PREPARE_FOR_SUBMISSION")]), env, []),
            "locked version": (lambda: CreativeFake(state="WAITING_FOR_REVIEW", attached="B9",
                                                    library=[self.image("H", header, "PREPARE_FOR_SUBMISSION")]), env, []),
            "approved on a locked version": (lambda: CreativeFake(
                state="WAITING_FOR_REVIEW", attached="B9",
                library=[self.image("H", header, "APPROVED"), self.image("S", search, "IN_REVIEW")]), env, []),
            "images never in the version's draft": (lambda: CreativeFake(drafts=["EMPTY"], draft_items={"EMPTY": []}),
                                                    submit, self.build()),
            "a creative draft is not the version's": (lambda: CreativeFake(
                library=[self.image("H", header, "WAITING_FOR_REVIEW"), self.image("S", search, "APPROVED")],
                placements={"P": {"type": "APP_STORE_SEARCH_RESULTS_ASSET", "image": "S", "loc": "LOC1"}},
                drafts=["IMAGES"], draft_items={"IMAGES": [image_item("OTHER")]}), submit, self.build()),
            "ready for review without its draft": (lambda: CreativeFake(
                state="READY_FOR_REVIEW", library=[self.image("H", header, "PREPARE_FOR_SUBMISSION")],
                drafts=["MIXED"], draft_items={"MIXED": [version_item(), image_item("X")]}), submit, self.build()),
            "the version's draft is reserved": (lambda: CreativeFake(
                state="READY_FOR_REVIEW", drafts=["EMPTY", "MINE"], draft_items={"EMPTY": [], "MINE": [version_item()]},
                library=[self.image("H", header, "APPROVED"), self.image("S", search, "IN_REVIEW")],
                placements={"P": {"type": "PRODUCT_PAGE_HEADER_ASSET", "image": "H", "loc": "LOC1"}}), submit,
                self.build()),
            "a rerun finishes a partial draft": (lambda: CreativeFake(
                library=[self.image("H", header, "READY_FOR_REVIEW"), self.image("S", search, "PREPARE_FOR_SUBMISSION")],
                drafts=["EMPTY", "PART"], draft_items={"EMPTY": [], "PART": [image_item("H")]}), env, []),
            "another draft is left alone": (lambda: CreativeFake(
                library=[self.image("S", search, "PREPARE_FOR_SUBMISSION")], drafts=["OTHER"],
                draft_items={"OTHER": [version_item("V0")]}), env, []),
        }
        for label, (factory, case_env, extra) in cases.items():
            with self.subTest(label=label):
                self.compare(factory, case_env, ["--version", VERSION, *extra, "--creative-dir", str(self.creative)])
        with self.subTest(label="processing timeout"):
            def timeout(module, recording):
                return [mock.patch.object(module, "CREATIVE_PROCESSING_TIMEOUT_SECONDS", 0)]

            self.compare(lambda: CreativeFake(library=[self.image("H", header, "UPLOAD_COMPLETE")],
                                              processing={"H": ["UPLOAD_COMPLETE"] * 5}), env,
                         ["--version", VERSION, "--creative-dir", str(self.creative)], timeout)

    def test_dry_runs(self) -> None:
        for env, argv in (({"UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "SUBMIT_IOS_REVIEW": "true"},
                           ["--metadata-dir", str(self.tmp / "meta"), "--build-id", "DRY-RUN", "--build-number", "1"]),
                          ({"UPLOAD_CREATIVE_ASSETS": "true"}, ["--creative-dir", str(self.creative)]),
                          ({"UPLOAD_CREATIVE_ASSETS": "true"}, []),
                          ({"UPLOAD_WHATS_NEW": "true"}, ["--metadata-dir", str(self.tmp / "nowhere")]),
                          ({"UPLOAD_LISTING": "true"}, ["--bundle-id", "com.example.other"])):
            with self.subTest(env=env, argv=argv):
                self.compare(FakeAsc, env, ["--dry-run", *argv])


class CreateVersionParityTests(ParityCase):
    def test_creation_scenarios(self) -> None:
        cases = [
            (None, {}, VERSION, []),
            (None, {"CREATE_IOS_VERSION": "true"}, VERSION, []),
            ([("7.1.2", "READY_FOR_SALE"), (VERSION, "PREPARE_FOR_SUBMISSION")], {"CREATE_IOS_VERSION": "true"}, VERSION, []),
            ([("7.1.2", "READY_FOR_SALE"), ("7.1.3", "IN_REVIEW")], {"CREATE_IOS_VERSION": "true"}, VERSION, []),
            ([("7.10", "READY_FOR_SALE")], {"CREATE_IOS_VERSION": "true"}, VERSION, []),
            ([("1.0", "REPLACED_WITH_NEW_VERSION"), ("7.1.10", "READY_FOR_SALE")], {"CREATE_IOS_VERSION": "true"},
             VERSION, []),
            ([(VERSION, "PREPARE_FOR_SUBMISSION"), (VERSION, "READY_FOR_SALE")], {"CREATE_IOS_VERSION": "true"},
             VERSION, []),
            (None, {"CREATE_IOS_VERSION": "true"}, "7.2-beta", []),
            (None, {"CREATE_IOS_VERSION": "true"}, None, []),
            (None, {"CREATE_IOS_VERSION": "true"}, VERSION, ["--dry-run"]),
            (None, {"CREATE_IOS_VERSION": "true"}, VERSION, ["--bundle-id", "com.example.other"]),
        ]
        for versions, env, version, extra in cases:
            for app_id, listed in (("6758935726", True), ("1234567890", True), ("6758935726", False)):
                argv = ["--create-version", *extra] + (["--version", version] if version else [])
                runs = []
                for module in (self.up.asc_release, asc):
                    fake = VersionCreationClient(versions=versions, app_id=app_id, listed_after_create=listed)
                    recording = Recording(fake)
                    full_env = {k: v for k, v in os.environ.items() if k not in FLAGS}
                    full_env.update(env)
                    with mock.patch.dict(os.environ, full_env, clear=True), \
                            mock.patch.object(sys, "argv", ["asc_release.py", *argv]), \
                            mock.patch.object(module, "client_from_environment", return_value=recording):
                        runs.append((outcome(module.main), recording.log))
                with self.subTest(versions=versions, env=env, version=version, extra=extra, app_id=app_id,
                                  listed=listed):
                    self.assertEqual(runs[1], runs[0])


class PreflightParityTests(ParityCase):
    def preflight(self, ios_module, asc_module, client, **kwargs):
        return outcome(lambda: ios_module.version_preflight(client, asc_module, version=VERSION, **kwargs))

    def test_states_metadata_and_drafts(self) -> None:
        for state in ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "READY_FOR_REVIEW", "WAITING_FOR_REVIEW",
                      "IN_REVIEW", "READY_FOR_SALE", ""):
            for localization in (True, False):
                for metadata, submit in ((True, True), (False, True), (True, False)):
                    results = []
                    for ios_module, asc_module in ((self.up.ios_release, self.up.asc_release), (ios_release, asc)):
                        client = PreflightClient([asc_version(state)], localization=localization)
                        results.append((self.preflight(ios_module, asc_module, client, metadata=metadata,
                                                       submit=submit), client.paths))
                    with self.subTest(state=state, localization=localization, metadata=metadata, submit=submit):
                        self.assertEqual(results[1], results[0])
        results = []
        for ios_module, asc_module in ((self.up.ios_release, self.up.asc_release), (ios_release, asc)):
            client = PreflightClient([asc_version("READY_FOR_SALE", "7.1")])
            results.append(self.preflight(ios_module, asc_module, client, metadata=False, submit=True))
        self.assertEqual(results[1], results[0])

    def test_creative_preflight(self) -> None:
        creative = self.tmp / "creative"
        header = creative / "header" / "header.png"
        header.parent.mkdir(parents=True)
        header.write_bytes(creative_png(3840, 1646))

        def image(state):
            return {"id": "H", "attributes": {"fileName": "header.png", "fileSize": header.stat().st_size,
                                              "state": state, "createdDate": "2026-10-20"}}

        cases = [("PREPARE_FOR_SUBMISSION", [], [], {}), ("READY_FOR_REVIEW", [image("PREPARE_FOR_SUBMISSION")],
                                                         ["MINE"], {"MINE": [version_item()]}),
                 ("READY_FOR_REVIEW", [image("APPROVED")], ["MINE"], {"MINE": [version_item()]}),
                 ("READY_FOR_REVIEW", [image("IN_REVIEW")], [], {}),
                 ("PREPARE_FOR_SUBMISSION", [image("REJECTED")], [], {})]
        for state, library, drafts, items in cases:
            for submit in (True, False):
                results = []
                for ios_module, asc_module in ((self.up.ios_release, self.up.asc_release), (ios_release, asc)):
                    fake = CreativeFake(state=state, library=library, drafts=drafts, draft_items=items)
                    results.append((self.preflight(ios_module, asc_module, fake, metadata=True, submit=submit,
                                                   creative=True, creative_dir=creative), fake.calls))
                with self.subTest(state=state, library=library, submit=submit):
                    self.assertEqual(results[1], results[0])


class BuildWaitParityTests(ParityCase):
    PAYLOADS = [
        {"data": []},
        {"data": [build_row(state="PROCESSING")], "included": [pre_row()]},
        {"data": [build_row()], "included": [pre_row()]},
        {"data": [build_row(state="FAILED")], "included": [pre_row()]},
        {"data": [build_row(state="INVALID")], "included": [pre_row()]},
        {"data": [build_row(expired=True)], "included": [pre_row()]},
        {"data": [build_row("B1"), build_row("B2")], "included": [pre_row()]},
        {"data": [build_row(uploaded=STARTED - 3600)], "included": [pre_row()]},
        {"data": [build_row(uploaded=STARTED - 300)], "included": [pre_row()]},
        {"data": [build_row(uploaded=None)], "included": [pre_row()]},
        {"data": [build_row(uploaded_raw="yesterday")], "included": [pre_row()]},
        {"data": [build_row(uploaded_raw=1_800_000_060)], "included": [pre_row()]},
        {"data": [build_row(app="OTHER")], "included": [pre_row()]},
        {"data": [build_row(number="41")], "included": [pre_row()]},
        {"data": [build_row("BX", pre="P2")], "included": [pre_row("P2", "7.1")]},
        {"data": [build_row("BY", pre="P3")], "included": [pre_row("P3", VERSION, "MAC_OS")]},
    ]

    def test_evaluate_builds(self) -> None:
        for payload in self.PAYLOADS:
            kwargs = dict(app_id=APP, marketing_version=VERSION, build_number="40", uploaded_after=STARTED)
            with self.subTest(payload=payload):
                self.assertEqual(outcome(lambda: ios_release.evaluate_builds(payload, **kwargs)),
                                 outcome(lambda: self.up.ios_release.evaluate_builds(payload, **kwargs)))

    def test_wait_for_build(self) -> None:
        scripts = [
            [{"data": []}, {"data": [build_row(state="PROCESSING")], "included": [pre_row()]},
             {"data": [build_row()], "included": [pre_row()]}],
            [{"data": [build_row(state="PROCESSING")], "included": [pre_row()]}],
            [asc.AscHttpError(503, "busy"), {"data": [build_row()], "included": [pre_row()]}],
            [asc.AscHttpError(401, "unauthorized")],
            [{"data": [build_row(state="FAILED")], "included": [pre_row()]}],
        ]
        for script in scripts:
            for start, minutes in ((STARTED + 120, 120), (STARTED, 10), (STARTED + 121 * 60, 120)):
                runs = []
                for module in (self.up.ios_release, ios_release):
                    client, clock = SequenceClient(script), FakeClock(start)
                    runs.append((outcome(lambda: module.wait_for_build(
                        client, app_id=APP, marketing_version=VERSION, build_number="40", uploaded_after=STARTED,
                        deadline=STARTED + minutes * 60, poll_seconds=30, now=clock.now, sleep=clock.sleep,
                        is_transient=lambda exc: getattr(exc, "status", 0) == 503)), client.paths, clock.sleeps))
                with self.subTest(script=script, start=start, minutes=minutes):
                    self.assertEqual(runs[1], runs[0])

    def test_next_build_number_and_retry_rule(self) -> None:
        for project, versions in ((39, ["13", "35", "12"]), (39, ["13", "39"]), (1, []), (5, ["7.1", "abc", "9"]),
                                  (5, ["٩٩"])):
            self.assertEqual(ios_release.next_build_number(project, versions),
                             self.up.ios_release.next_build_number(project, versions))
        import http.client
        import urllib.error

        for exc in (http.client.RemoteDisconnected("x"), TimeoutError("x"), asc.AscHttpError(429, "x"),
                    urllib.error.HTTPError("https://x", 502, "x", {}, None), ValueError("x"), SystemExit(1)):
            self.assertEqual(ios_release.is_transient_error(exc), self.up.ios_release.is_transient_error(exc))


class ExportComplianceParityTests(ParityCase):
    KEY = "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption"
    CODE = {"INFOPLIST_KEY_ITSEncryptionExportComplianceCode": "ABC123"}
    GENERATED = {"GENERATE_INFOPLIST_FILE": "YES"}
    APP_PLIST = {"INFOPLIST_FILE": "calorietracker/Info.plist", **GENERATED}

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "project"
        (self.root / "calorietracker.xcodeproj").mkdir(parents=True)
        (self.root / "calorietracker").mkdir()

    def plist(self, values: dict, *, binary: bool = False, name: str = "calorietracker/Info.plist") -> Path:
        path = self.root / name
        path.write_bytes(plistlib.dumps({"CFBundleName": "App", **values},
                                        fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML))
        return path

    def project(self, app: dict, *, project: dict | None = None, other: dict | None = None) -> Path:
        ids = iter(f"{n:024X}" for n in range(1, 1000))
        objects: list[str] = []

        def config_list(owner: str, configs: dict) -> str:
            list_id = next(ids)
            refs = []
            for name, settings in configs.items():
                config_id = next(ids)
                refs.append(f"\t\t\t\t{config_id} /* {name} */,\n")
                lines = "".join(f"\t\t\t\t{key} = {value};\n" for key, value in settings.items())
                objects.append(f"\t\t{config_id} /* {name} */ = {{\n\t\t\tisa = XCBuildConfiguration;\n"
                               f"\t\t\tbuildSettings = {{\n{lines}\t\t\t}};\n\t\t\tname = {name};\n\t\t}};\n")
            objects.append(f"\t\t{list_id} /* Build configuration list for {owner} */ = {{\n"
                           f"\t\t\tisa = XCConfigurationList;\n\t\t\tbuildConfigurations = (\n{''.join(refs)}\t\t\t);\n"
                           "\t\t\tdefaultConfigurationName = Release;\n\t\t};\n")
            return list_id

        def target(name: str, configs: dict) -> None:
            list_id = config_list(f'PBXNativeTarget "{name}"', configs)
            objects.append(f"\t\t{next(ids)} /* {name} */ = {{\n\t\t\tisa = PBXNativeTarget;\n"
                           f"\t\t\tbuildConfigurationList = {list_id} /* Build configuration list */;\n"
                           f"\t\t\tname = {name};\n\t\t}};\n")

        target("calorietracker", app)
        if other is not None:
            target("FudAIWatchApp", other)
        project_list = config_list('PBXProject "calorietracker"', project or {"Debug": {}, "Release": {}})
        objects.append(f"\t\t{next(ids)} /* Project object */ = {{\n\t\t\tisa = PBXProject;\n"
                       f"\t\t\tbuildConfigurationList = {project_list} /* Build configuration list */;\n\t\t}};\n")
        path = self.root / "calorietracker.xcodeproj" / "project.pbxproj"
        path.write_text("// !$*UTF8*$!\n{\n\tobjects = {\n" + "".join(objects) + "\t};\n}\n", encoding="utf-8")
        return path

    def both(self, settings: dict) -> dict:
        return {"Debug": dict(settings), "Release": dict(settings)}

    def same(self, app: dict, plist: dict | None = None, *, target: str = "calorietracker", **kwargs) -> None:
        if plist is not None:
            self.plist(plist)
        pbx = self.project(app, **kwargs)
        self.assertEqual(outcome(lambda: ios_release.check_export_compliance(pbx, target)),
                         outcome(lambda: self.up.ios_release.check_export_compliance(pbx, target)))

    def test_the_project_matrix(self) -> None:
        cases = [
            (self.both(self.APP_PLIST), {"ITSAppUsesNonExemptEncryption": False}, {}),
            (self.both({**self.GENERATED, self.KEY: "NO"}), None, {}),
            (self.both(self.GENERATED), None, {"project": self.both({self.KEY: "NO"})}),
            (self.both({"INFOPLIST_FILE": "\"$(SRCROOT)/calorietracker/Info.plist\""}),
             {"ITSAppUsesNonExemptEncryption": False}, {}),
            ({"Debug": {**self.GENERATED, self.KEY: "NO"}, "Release": dict(self.GENERATED)}, None, {}),
            (self.both(self.APP_PLIST), {}, {}),
            (self.both({"GENERATE_INFOPLIST_FILE": "NO", self.KEY: "NO"}), None, {}),
            (self.both(self.GENERATED), None, {"other": self.both({**self.GENERATED, self.KEY: "NO"})}),
            (self.both({**self.GENERATED, self.KEY: "YES"}), None, {}),
            (self.both({**self.GENERATED, self.KEY: "YES", **self.CODE}), None, {}),
            (self.both(self.APP_PLIST), {"ITSAppUsesNonExemptEncryption": True,
                                         "ITSEncryptionExportComplianceCode": " "}, {}),
            (self.both({**self.GENERATED, self.KEY: "maybe"}), None, {}),
            (self.both({**self.GENERATED, self.KEY: "NO", f'"{self.KEY}[sdk=iphoneos*]"': "YES", **self.CODE}), None, {}),
            (self.both(self.APP_PLIST), {"ITSAppUsesNonExemptEncryption": "NO"}, {}),
            ({"Debug": {**self.GENERATED, self.KEY: "NO"}, "Release": {**self.GENERATED, self.KEY: "YES", **self.CODE}},
             None, {}),
            (self.both({"INFOPLIST_FILE": "calorietracker/Missing.plist"}), None, {}),
            (self.both({"INFOPLIST_FILE": "$(OTHER_DIR)/Info.plist"}), None, {}),
            ({}, None, {}),
        ]
        for app, plist, kwargs in cases:
            with self.subTest(app=app, plist=plist, kwargs=kwargs):
                self.same(app, plist, **kwargs)
        with self.subTest("missing target"):
            self.same(self.both(self.GENERATED), target="Missing")

    def test_archived_plists(self) -> None:
        broken = self.root / "Broken.plist"
        broken.write_bytes(b"not a plist")
        for path in (self.plist({"ITSAppUsesNonExemptEncryption": False}, binary=True),
                     self.plist({"ITSAppUsesNonExemptEncryption": True, "ITSEncryptionExportComplianceCode": "A"},
                                binary=True, name="yes.plist"),
                     self.plist({}, binary=True, name="none.plist"),
                     self.plist({"ITSAppUsesNonExemptEncryption": True}, binary=True, name="nocode.plist"),
                     broken, self.root / "missing" / "Info.plist"):
            with self.subTest(path=path.name):
                self.assertEqual(outcome(lambda: ios_release.check_archived_export_compliance(path)),
                                 outcome(lambda: self.up.ios_release.check_archived_export_compliance(path)))


if __name__ == "__main__":
    unittest.main()
