"""Android additions: the read-only Play client, the called-workflow re-run job name, gradle-version."""
from __future__ import annotations

import argparse
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from store_release.android import release as ar
from support import apps as support_apps

support_apps.configure_fud_ai_defaults()


class Recorder:
    """A fake androidpublisher service: every call path is recorded; execute() returns canned data."""

    def __init__(self, results: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}

    def __getattr__(self, name):
        return Node(self, name)


class Node:
    def __init__(self, root: Recorder, path: str) -> None:
        self.root = root
        self.path = path

    def __getattr__(self, name):
        return Node(self.root, f"{self.path}.{name}")

    def __call__(self, **kwargs):
        # Like the Google API client: edits(), edits().tracks() return resources;
        # a method called with arguments returns a request to execute.
        self.root.calls.append((self.path, kwargs))
        return Request(self.root, self.path) if kwargs else Node(self.root, self.path)


class Request:
    def __init__(self, root: Recorder, path: str) -> None:
        self.root = root
        self.path = path

    def execute(self):
        self.root.calls.append((self.path + ".execute", {}))
        return self.root.results.get(self.path, {})


class ReadOnlyPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Recorder({"edits.insert": {"id": "E1"}, "edits.tracks.list": {"tracks": []}})
        self.client = ar.ReadOnlyPublisher(self.service)

    def test_reads_in_its_own_edit_are_allowed(self) -> None:
        edits = self.client.edits()
        self.assertEqual(edits.insert(packageName="p", body={}).execute(), {"id": "E1"})
        self.assertEqual(self.client.edit_ids, ["E1"])
        edits.tracks().list(packageName="p", editId="E1").execute()
        edits.tracks().get(packageName="p", editId="E1", track="internal").execute()
        edits.bundles().list(packageName="p", editId="E1").execute()
        edits.apks().list(packageName="p", editId="E1").execute()
        edits.listings().get(packageName="p", editId="E1", language="en-US").execute()
        edits.listings().list(packageName="p", editId="E1").execute()
        edits.images().list(packageName="p", editId="E1", language="en-US", imageType="phoneScreenshots").execute()
        edits.details().get(packageName="p", editId="E1").execute()
        edits.delete(packageName="p", editId="E1").execute()
        self.assertIn(("edits.delete", {"packageName": "p", "editId": "E1"}), self.service.calls)

    def test_every_write_is_refused_before_it_is_sent(self) -> None:
        edits = self.client.edits()
        refused = [
            lambda: edits.commit(packageName="p", editId="E1"),
            lambda: edits.validate(packageName="p", editId="E1"),
            lambda: edits.listings().update(packageName="p", editId="E1", language="en-US", body={}),
            lambda: edits.listings().patch(packageName="p", editId="E1", language="en-US", body={}),
            lambda: edits.images().upload(packageName="p", editId="E1"),
            lambda: edits.images().deleteall(packageName="p", editId="E1"),
            lambda: edits.images().delete(packageName="p", editId="E1"),
            lambda: edits.tracks().update(packageName="p", editId="E1", track="internal", body={}),
            lambda: edits.tracks().patch(packageName="p", editId="E1", track="internal", body={}),
            lambda: edits.bundles().upload(packageName="p", editId="E1"),
            lambda: edits.apks().upload(packageName="p", editId="E1"),
            lambda: edits.details().update(packageName="p", editId="E1", body={}),
            lambda: edits.testers(),
            lambda: self.client.reviews(),
            lambda: self.client.inappproducts(),
            lambda: edits.delete(packageName="p", editId="SOMEONE-ELSES"),
        ]
        for call in refused:
            with self.subTest(call=call), self.assertRaises(ar.ReleaseError) as caught:
                call()
            self.assertIn("read-only Play client refused", str(caught.exception))
        # Resource accessors (edits(), edits().images()) build nothing; no request was made or sent.
        sent = [path for path, kwargs in self.service.calls if kwargs or path.endswith(".execute")]
        self.assertEqual(sent, [], "nothing reached the service")

    def test_dunder_probes_are_plain_attribute_errors(self) -> None:
        self.assertFalse(hasattr(self.client, "__wrapped__"))
        self.assertFalse(hasattr(self.client.edits(), "__iter__"))

    def test_play_publisher_wraps_when_asked(self) -> None:
        with mock.patch.dict(os.environ, {"PLAY_SERVICE_ACCOUNT_JSON": "{}"}), \
                mock.patch("store_release.android.play_listing.build_android_publisher", return_value=Recorder()):
            self.assertIsInstance(ar.play_publisher(read_only=True), ar.ReadOnlyPublisher)
            self.assertIsInstance(ar.play_publisher(), Recorder)

    def test_preflight_and_read_back_use_the_read_only_client(self) -> None:
        asked = []

        def publisher(*, read_only=False):
            asked.append(read_only)
            raise ar.ReleaseError("stop")

        with mock.patch.object(ar, "play_publisher", side_effect=publisher), \
                mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1"}), redirect_stdout(io.StringIO()):
            for command in (ar.cmd_play_preflight, ar.cmd_check_tracks):
                with self.assertRaises(ar.ReleaseError):
                    command(argparse.Namespace(tracks="internal"))
        self.assertEqual(asked, [True, True])


class CalledWorkflowRerunTests(unittest.TestCase):
    """D12: inside a called workflow the jobs API names the release job "release / release"."""

    def test_the_called_job_name_is_recognised(self) -> None:
        upload = [{"name": "Upload to Play (internal)", "conclusion": "success"}]
        jobs = {1: [{"name": "release / release", "steps": upload}],
                2: [{"name": "release / release", "steps": [{"name": "Upload to Play", "conclusion": "skipped"}]}]}
        self.assertEqual(ar.earlier_upload_attempt(jobs.get, 3), 1)
        for name in ("gates / gates", "release / release-notes", "prerelease", "quality / android", "my release"):
            with self.subTest(name=name):
                self.assertIsNone(ar.earlier_upload_attempt({1: [{"name": name, "steps": upload}]}.get, 2))
        self.assertEqual(ar.earlier_upload_attempt({1: [{"name": "release", "steps": upload}]}.get, 2), 1)


class GradleVersionCommandTests(unittest.TestCase):
    def test_prints_the_synthetic_version(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            ar.main(["gradle-version"])
        self.assertEqual(out.getvalue(), "android/app/build.gradle.kts: versionName 7.2, versionCode 42\n")
        with mock.patch.object(ar, "gradle_version", return_value=("7.x", 42)), \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            ar.main(["gradle-version"])
        self.assertIn("declares versionName '7.x'", err.getvalue())

    def test_messages_name_the_configured_gradle_file(self) -> None:
        with self.assertRaisesRegex(ar.ReleaseError, "android/app/build.gradle.kts must declare exactly one"):
            ar.gradle_version("")
        with self.assertRaisesRegex(ar.ReleaseError, "update android.upload_cert_sha256 only after"):
            ar.check_upload_certificate("0" * 64, "1" * 64)


if __name__ == "__main__":
    unittest.main()


# --- the read-only store plan (store_release.android.plan) -------------------------------------

import json as _json  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from store_release.android import plan as android_plan  # noqa: E402
from support import pngs as _pngs  # noqa: E402
from support.play_fakes import FakePlay  # noqa: E402


def gh_releases(releases=(("android-v7.1.1", "Android 7.1.1 (versionCode 41)"),), existing=()):
    def run(argv):
        argv = list(argv)
        if argv[:3] == ["gh", "release", "list"]:
            return _subprocess.CompletedProcess(argv, 0, _json.dumps([{"tagName": t, "name": n} for t, n in releases]), "")
        if argv[:3] == ["gh", "release", "view"]:
            return _subprocess.CompletedProcess(argv, 0 if argv[3] in existing else 1, "", "")
        raise AssertionError(f"a store plan ran {argv}")
    return run


class AndroidStorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = _tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = _Path(tmp.name)
        self.app = support_apps.fixture_app("fud-ai", self.tmp / "app")
        configured = support_apps.configured(self.app)
        configured.__enter__()
        self.addCleanup(configured.__exit__, None, None, None)

    def plan(self, switches: dict, service=None, *, gh=None, env=None) -> tuple[int, str, dict, FakePlay | None]:
        environ = {"ANDROID_RELEASE_AUTOMATION": "true", "GITHUB_REPOSITORY": "apoorvdarshan/fud-ai", **switches,
                   **(env or {})}
        out = io.StringIO()
        with redirect_stdout(out):
            code, result = android_plan.run_plan(self.app, work_dir=self.tmp / "plan", environ=environ,
                                                 service=service, gh_runner=gh or gh_releases())
        if isinstance(service, FakePlay):
            self.assertEqual(service.writes(), [], "the plan only reads Google Play")
            self.assertIn("edits.delete", [name for name, _ in service.calls], "the throwaway edit is deleted")
        return code, out.getvalue(), result, service

    def test_a_new_version_code_and_a_differing_listing(self) -> None:
        play = FakePlay(bundles=[40, 41], tracks={"internal": [(41, "completed")]},
                        listing={"title": "Old title", "shortDescription": "Track food with AI in seconds.",
                                 "fullDescription": "x"})
        code, out, result, _ = self.plan({"ANDROID_INTERNAL_TESTING": "true", "ANDROID_UPLOAD_LISTING": "true"}, play)
        self.assertEqual(code, 0)
        self.assertIn("versionCode 42 is higher than every earlier Android GitHub Release (highest: 41)", out)
        self.assertIn("create android-v7.2 titled 'Android 7.2 (versionCode 42)' with --prerelease --latest=false", out)
        self.assertIn("title differs (9 → 13 chars)", out)
        self.assertIn("shortDescription same", out)
        self.assertIn("would upload versionCode 42 to internal (highest on Play: 41)", out)
        self.assertEqual(out.splitlines()[-1], "store-plan: CLEAN")
        self.assertEqual(result["reads"], ["apks", "bundles", "listing", "tracks"])

    def test_an_old_version_code_is_a_problem(self) -> None:
        play = FakePlay(bundles=[42], tracks={"production": [(42, "completed")]})
        code, out, _, _ = self.plan({"ANDROID_PRODUCTION_ROLLOUT": "true"}, play)
        self.assertEqual(code, 0)
        self.assertIn("refused: versionCode 42 is not new to Google Play", out)
        self.assertIn("with --prerelease=false --latest", out)
        self.assertEqual(out.splitlines()[-1], "store-plan: 1 PROBLEM(S)")

    def test_screenshots_are_compared_with_the_live_set(self) -> None:
        frame = _pngs.rgb(1080, 1920)
        for i in (1, 2):
            _pngs.write(self.app.root / "store/screenshots/play" / f"{i:02d}-play.png", frame)
        import hashlib

        play = FakePlay(images=[hashlib.sha256(frame).hexdigest(), "f" * 64, "e" * 64])
        code, out, _, _ = self.plan({"ANDROID_UPLOAD_SCREENSHOTS": "true"}, play)
        self.assertEqual(code, 0, out)
        self.assertIn("would delete 3 live and upload 2 staged frames (2 staged frame(s) already live by sha256)", out)

    def test_without_credentials_the_play_part_is_skipped(self) -> None:
        code, out, _, _ = self.plan({"ANDROID_INTERNAL_TESTING": "true"}, None, env={"PLAY_SERVICE_ACCOUNT_JSON": ""})
        self.assertEqual(code, 0)
        self.assertIn("skipped: no Play credentials", out)
        self.assertIn("store-plan: CLEAN", out)

    def test_master_off_and_the_github_preflight(self) -> None:
        code, out, _, _ = self.plan({"ANDROID_RELEASE_AUTOMATION": "false"}, None)
        self.assertIn("a tag run builds and releases nothing", out)
        code, out, _, _ = self.plan({}, None, gh=gh_releases(releases=(("android-v7.3", "Android 7.3 (versionCode 50)"),)))
        self.assertIn("refused: versionCode 42 is not higher than versionCode 50 of the android-v7.3 GitHub Release", out)
        self.assertIn("GitHub Release: not planned (blocked by problem 1)", out)

    def test_play_errors_abort_the_plan(self) -> None:
        class Broken(FakePlay):
            def edits(self):
                raise RuntimeError("HTTP 401 unauthorized")

        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(android_plan.PlanAbort, "401"):
            android_plan.run_plan(self.app, work_dir=self.tmp / "p", environ={
                "ANDROID_RELEASE_AUTOMATION": "true", "ANDROID_INTERNAL_TESTING": "true"}, service=Broken(),
                gh_runner=gh_releases())
        app = mock.Mock(config=self.app.config, root=self.app.root)
        with mock.patch.object(android_plan, "run_plan", side_effect=android_plan.PlanAbort("Google Play: 403")), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(android_plan.main(app, ["--work-dir", str(self.tmp / "q")]), 1)
        self.assertIn("could not be computed: Google Play: 403", err.getvalue())
