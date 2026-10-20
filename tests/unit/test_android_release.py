"""Offline tests for android release: switches, artifact checks, Play read-back.

Ported from fud-ai a99591908 scripts/store/test_android_release.py (imports and the
tests that read real fud-ai files changed; tests/unit/PORTED_FROM_FUD_AI.txt).
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release.android import release as ar
from store_release.text import whats_new as prepare_whats_new
from support import apps as support_apps

support_apps.configure_fud_ai_defaults()

ALL_ON = {name: "true" for name, _ in ar.OPERATION_SWITCHES}


def gates(env=None, *, event="push", ref="android-v7.2", version="7.2"):
    return ar.resolve_gates(env or {}, event=event, ref_name=ref, version_name=version)


def pem(der: bytes) -> str:
    body = base64.b64encode(der).decode()
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"


UPLOAD_DER = b"upload-certificate"
UPLOAD = hashlib.sha256(UPLOAD_DER).hexdigest()
DEBUG = hashlib.sha256(b"debug-certificate").hexdigest()
IDENTITY = {"package": ar.PACKAGE_NAME, "version_code": 42, "version_name": "7.2"}


class SwitchTests(unittest.TestCase):
    def test_missing_settings_mean_nothing_runs(self):
        result = gates()
        self.assertFalse(result["build"])
        self.assertFalse(result["github_release"])
        self.assertFalse(result["play_upload"])
        self.assertEqual(result["play_tracks"], "")
        self.assertTrue(any("is off" in note for note in result["notes"]))

    def test_master_off_ignores_every_operation(self):
        result = gates(ALL_ON)
        for _, key in ar.OPERATION_SWITCHES:
            self.assertFalse(result[key], key)
        self.assertFalse(result["play_upload"])
        self.assertTrue(any("ANDROID_PRODUCTION_ROLLOUT" in note for note in result["notes"]))

    def test_master_alone_builds_and_releases_on_github_only(self):
        result = gates({ar.MASTER: "true"})
        self.assertTrue(result["build"])
        self.assertTrue(result["github_release"])
        self.assertFalse(result["play_upload"])
        self.assertFalse(result["listing"])

    def test_invalid_values_fail_even_when_master_is_off(self):
        for name, _ in ar.OPERATION_SWITCHES:
            with self.subTest(name=name), self.assertRaises(ar.ReleaseError):
                gates({name: "maybe"})
        with self.assertRaises(ar.ReleaseError):
            gates({ar.MASTER: "enabled"})

    def test_switch_values_are_case_and_space_insensitive(self):
        result = gates({ar.MASTER: " YES ", "ANDROID_INTERNAL_TESTING": "On"})
        self.assertEqual(result["play_tracks"], "internal")
        result = gates({ar.MASTER: "1", "ANDROID_INTERNAL_TESTING": "0"})
        self.assertFalse(result["play_upload"])

    def test_each_track_switch_maps_to_its_play_track(self):
        for name, track in (
            ("ANDROID_INTERNAL_TESTING", "internal"),
            ("ANDROID_CLOSED_TESTING", "alpha"),
            ("ANDROID_PRODUCTION_ROLLOUT", "production"),
        ):
            with self.subTest(name=name):
                result = gates({ar.MASTER: "true", name: "true"})
                self.assertTrue(result["play_upload"])
                self.assertEqual(result["play_tracks"], track)

    def test_testing_tracks_never_imply_production(self):
        result = gates(
            {ar.MASTER: "true", "ANDROID_INTERNAL_TESTING": "true", "ANDROID_CLOSED_TESTING": "true"}
        )
        self.assertEqual(result["play_tracks"], "internal,alpha")
        self.assertFalse(result["production"])

    def test_production_output_decides_latest_github_release(self):
        # The workflow marks the GitHub Release Latest only when `production` is true;
        # otherwise it is a pre-release so the iOS release stays Latest.
        for env, expected in (
            ({ar.MASTER: "true"}, "false"),
            ({ar.MASTER: "true", "ANDROID_INTERNAL_TESTING": "true"}, "false"),
            ({ar.MASTER: "true", "ANDROID_PRODUCTION_ROLLOUT": "true"}, "true"),
            ({"ANDROID_PRODUCTION_ROLLOUT": "true"}, "false"),
        ):
            with self.subTest(env=env), tempfile.NamedTemporaryFile("r", suffix=".out") as out, \
                    mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out.name}), redirect_stdout(io.StringIO()):
                resolved = gates(env)
                resolved.pop("notes")
                ar.write_outputs(resolved)
                self.assertIn(f"production={expected}\n", Path(out.name).read_text())

    def test_all_tracks_go_in_one_upload(self):
        result = gates({ar.MASTER: "true", **ALL_ON})
        self.assertEqual(result["play_tracks"], "internal,alpha,production")
        self.assertTrue(result["whats_new"])

    def test_listing_and_screenshots_need_no_track(self):
        result = gates(
            {ar.MASTER: "true", "ANDROID_UPLOAD_LISTING": "true", "ANDROID_UPLOAD_SCREENSHOTS": "true"}
        )
        self.assertTrue(result["listing"])
        self.assertTrue(result["screenshots"])
        self.assertFalse(result["play_upload"])

    def test_whats_new_without_a_track_is_skipped_with_a_note(self):
        result = gates({ar.MASTER: "true", "ANDROID_UPLOAD_WHATS_NEW": "true"})
        self.assertFalse(result["whats_new"])
        self.assertTrue(any("What's New" in note for note in result["notes"]))

    def test_tag_must_match_version_name(self):
        with self.assertRaises(ar.ReleaseError):
            gates({ar.MASTER: "true"}, ref="android-v7.1")
        with self.assertRaises(ar.ReleaseError):
            gates({ar.MASTER: "true"}, ref="ios-v7.2")
        with self.assertRaises(ar.ReleaseError):
            gates({ar.MASTER: "true"}, ref="android-v7.2.0")

    def test_manual_run_only_validates(self):
        result = gates({ar.MASTER: "true", **ALL_ON}, event="workflow_dispatch", ref="main")
        self.assertEqual(result["mode"], "validate")
        self.assertTrue(result["build"])
        self.assertFalse(result["github_release"])
        self.assertFalse(result["play_upload"])
        for _, key in ar.OPERATION_SWITCHES:
            self.assertFalse(result[key], key)

    def test_manual_run_still_rejects_invalid_values(self):
        with self.assertRaises(ar.ReleaseError):
            gates({"ANDROID_CLOSED_TESTING": "sure"}, event="workflow_dispatch", ref="main")

    def test_unsupported_event_fails(self):
        with self.assertRaises(ar.ReleaseError):
            gates({ar.MASTER: "true"}, event="pull_request")


class GradleVersionTests(unittest.TestCase):
    def test_reads_default_config_and_ignores_suffixes(self):
        text = """
            versionCode = 42
            versionName = "7.2"
            debug { versionNameSuffix = "-debug" }
        """
        self.assertEqual(ar.gradle_version(text), ("7.2", 42))

    def test_ambiguous_or_missing_version_fails(self):
        with self.assertRaises(ar.ReleaseError):
            ar.gradle_version('versionCode = 1\nversionName = "1.0"\nversionName = "1.1"\n')
        with self.assertRaises(ar.ReleaseError):
            ar.gradle_version('versionName = "1.0"\n')

    def test_repository_gradle_file_parses(self):
        name, code = ar.gradle_version(ar.GRADLE.read_text(encoding="utf-8"))
        self.assertRegex(name, r"^[0-9]+(\.[0-9]+){1,2}$")
        self.assertGreater(code, 0)


class ArtifactTests(unittest.TestCase):
    def test_pem_fingerprints_hash_the_der_bytes(self):
        text = "Signer #1:\n" + pem(UPLOAD_DER) + "noise\n" + pem(b"debug-certificate")
        self.assertEqual(ar.pem_fingerprints(text), {UPLOAD, DEBUG})

    def test_apksigner_fingerprints(self):
        output = (
            "Verifies\nVerified using v2 scheme (APK Signature Scheme v2): true\n"
            f"Signer #1 certificate DN: CN=Fud AI\nSigner #1 certificate SHA-256 digest: {UPLOAD.upper()}\n"
            "Signer #1 certificate SHA-1 digest: 0123\n"
        )
        self.assertEqual(ar.apksigner_fingerprints(output), {UPLOAD})

    def test_apksigner_fingerprints_newer_format_ignores_keys_and_stamps(self):
        output = (
            "Verifies\nNumber of signers: 1\n"
            f"V2 Signer: certificate SHA-256 digest: {UPLOAD}\n"
            f"V2 Signer: public key SHA-256 digest: {DEBUG}\n"
            f"Source Stamp Signer certificate SHA-256 digest: {DEBUG}\n"
        )
        self.assertEqual(ar.apksigner_fingerprints(output), {UPLOAD})

    def test_badging_identity(self):
        output = (
            "package: name='com.apoorvdarshan.calorietracker' versionCode='42' versionName='7.2' "
            "platformBuildVersionName='16'\nsdkVersion:'26'\n"
        )
        found = ar.badging_identity(output)
        self.assertEqual(found, (ar.PACKAGE_NAME, 42, "7.2"))
        ar.check_identity("APK", found, **IDENTITY)

    def test_badging_without_package_line_fails(self):
        with self.assertRaises(ar.ReleaseError):
            ar.badging_identity("sdkVersion:'26'\n")

    def test_manifest_identity(self):
        xml = (
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
            'package="com.apoorvdarshan.calorietracker" android:versionCode="42" '
            'android:versionName="7.2"><application/></manifest>'
        )
        found = ar.manifest_identity(xml)
        self.assertEqual(found, (ar.PACKAGE_NAME, 42, "7.2"))

    def test_bad_manifest_fails(self):
        with self.assertRaises(ar.ReleaseError):
            ar.manifest_identity("not xml")
        with self.assertRaises(ar.ReleaseError):
            ar.manifest_identity('<manifest package="x"/>')

    def test_wrong_app_or_version_is_rejected(self):
        for found in (
            ("com.apoorvdarshan.calorietracker.debug", 42, "7.2"),
            (ar.PACKAGE_NAME, 41, "7.2"),
            (ar.PACKAGE_NAME, 42, "7.2-debug"),
        ):
            with self.subTest(found=found), self.assertRaises(ar.ReleaseError):
                ar.check_identity("AAB", found, **IDENTITY)

    def test_only_the_upload_key_may_sign(self):
        ar.check_signers("APK", {UPLOAD}, upload_cert=UPLOAD)
        for found in (set(), {DEBUG}, {UPLOAD, DEBUG}):
            with self.subTest(found=found), self.assertRaises(ar.ReleaseError):
                ar.check_signers("APK", found, upload_cert=UPLOAD)


class SignatureCoverageTests(unittest.TestCase):
    LONG = "base/root/" + "x" * 80 + ".properties"

    def bundle(self, entries, manifest_names):
        manifest = "Manifest-Version: 1.0\r\n\r\n"
        for name in manifest_names:
            line = f"Name: {name}"
            # JAR manifests wrap at 72 bytes with a leading-space continuation.
            wrapped = "\r\n ".join([line[:72]] + [line[i:i + 71] for i in range(72, len(line), 71)])
            manifest += f"{wrapped}\r\nSHA-256-Digest: AAAA\r\n\r\n"
        handle = tempfile.NamedTemporaryFile(suffix=".aab", delete=False)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        with zipfile.ZipFile(handle.name, "w") as bundle:
            bundle.writestr("META-INF/MANIFEST.MF", manifest)
            bundle.writestr("META-INF/FUDAI.SF", "signature")
            bundle.writestr("META-INF/FUDAI.RSA", "block")
            bundle.writestr("base/", "")
            for name in entries:
                bundle.writestr(name, "data")
        return Path(handle.name)

    def test_fully_signed_bundle_passes(self):
        names = ["BundleConfig.pb", "base/manifest/AndroidManifest.xml", self.LONG]
        self.assertEqual(ar.unsigned_entries(self.bundle(names, names)), [])

    def test_added_unsigned_entry_is_reported(self):
        names = ["BundleConfig.pb", "base/dex/classes.dex"]
        found = ar.unsigned_entries(self.bundle(names + ["base/dex/classes2.dex"], names))
        self.assertEqual(found, ["base/dex/classes2.dex"])

    def test_bundle_without_manifest_is_entirely_unsigned(self):
        handle = tempfile.NamedTemporaryFile(suffix=".aab", delete=False)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        with zipfile.ZipFile(handle.name, "w") as bundle:
            bundle.writestr("BundleConfig.pb", "data")
        self.assertEqual(ar.unsigned_entries(Path(handle.name)), ["BundleConfig.pb"])


class TrackReadBackTests(unittest.TestCase):
    def fetcher(self, tracks):
        return lambda name: tracks[name]

    def test_completed_release_on_every_track_passes(self):
        tracks = {
            "internal": {"releases": [{"versionCodes": ["42"], "status": "completed"}]},
            "alpha": {
                "releases": [
                    {"versionCodes": ["41"], "status": "completed"},
                    {"versionCodes": ["42"], "status": "completed"},
                ]
            },
        }
        messages = ar.check_tracks(self.fetcher(tracks), ["internal", "alpha"], 42)
        self.assertEqual(len(messages), 2)
        self.assertIn("internal testing", messages[0])
        self.assertIn("closed testing", messages[1])
        self.assertIn("review", messages[1])

    def test_missing_or_unfinished_release_fails(self):
        for release in (
            {},
            {"releases": [{"versionCodes": ["41"], "status": "completed"}]},
            {"releases": [{"versionCodes": ["42"], "status": "draft"}]},
            {"releases": [{"versionCodes": ["42"], "status": "halted"}]},
        ):
            with self.subTest(release=release), self.assertRaises(ar.ReleaseError):
                ar.check_tracks(self.fetcher({"production": release}), ["production"], 42)


class UploadCertificateTests(unittest.TestCase):
    """A7: the keystore must hold the pinned upload certificate."""

    def test_pinned_value_is_the_fud_ai_upload_certificate(self):
        self.assertRegex(ar.UPLOAD_CERT_SHA256, r"^[0-9a-f]{64}$")
        # The configured android.upload_cert_sha256 is Fud AI's upload certificate.
        self.assertEqual(ar.UPLOAD_CERT_SHA256, "35d739f426248c3c04f344ffaaf972dabc67b9aaa64f49143ef8f3f4b1f704e0")
        ar.check_upload_certificate(ar.UPLOAD_CERT_SHA256, ar.UPLOAD_CERT_SHA256)

    def test_any_other_certificate_or_a_bad_pin_fails(self):
        with self.assertRaisesRegex(ar.ReleaseError, "not the pinned"):
            ar.check_upload_certificate(DEBUG, UPLOAD)
        for bad in ("", "abc", UPLOAD.upper()):
            with self.subTest(bad=bad), self.assertRaisesRegex(ar.ReleaseError, "not a SHA-256"):
                ar.check_upload_certificate(UPLOAD, bad)

    def test_verify_stops_before_reading_the_artifacts(self):
        import argparse
        import subprocess

        keytool = subprocess.CompletedProcess([], 0, stdout=pem(b"some-other-key"), stderr="")
        args = argparse.Namespace(
            aab=Path("app.aab"), apk=Path("app.apk"), keystore=Path("k.jks"), alias="fudai",
            bundletool=Path("bundletool.jar"), package=ar.PACKAGE_NAME, expected_cert=UPLOAD,
        )
        with mock.patch.object(ar, "run", return_value=keytool) as run, redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ar.ReleaseError, "not the pinned"):
            ar.cmd_verify(args)
        self.assertEqual(run.call_count, 1, "nothing else may run with an unexpected key")


class PlayTrackListTests(unittest.TestCase):
    """A8: the upload action falls back to production when given no track."""

    def test_valid_lists(self):
        self.assertEqual(ar.parse_tracks("internal,alpha"), ["internal", "alpha"])
        self.assertEqual(ar.parse_tracks("production"), ["production"])

    def test_empty_unknown_or_repeated_tracks_fail(self):
        with self.assertRaisesRegex(ar.ReleaseError, "default to production"):
            ar.parse_tracks("")
        for raw in ("beta", "internal,internal", "internal,Production"):
            with self.subTest(raw=raw), self.assertRaises(ar.ReleaseError):
                ar.parse_tracks(raw)


def track(name, *releases):
    return {"track": name, "releases": [{"versionCodes": [str(code)], "status": status} for code, status in releases]}


class PlayPreflightTests(unittest.TestCase):
    """A3/A4: a new versionCode is uploaded; only this run's own earlier upload is skipped."""

    def decide(self, code=43, *, known=(40, 41, 42), tracks=None, selected=("internal",), earlier=None):
        return ar.play_upload_decision(
            code, known_codes=set(known), tracks=tracks or {}, selected=list(selected),
            earlier_upload_attempt=earlier,
        )

    def test_new_version_code_is_uploaded(self):
        self.assertEqual(self.decide(43), "upload")
        self.assertEqual(self.decide(1, known=()), "upload")

    def test_old_or_reused_version_code_fails_before_the_build(self):
        with self.assertRaisesRegex(ar.ReleaseError, "bump versionCode"):
            self.decide(42)
        with self.assertRaisesRegex(ar.ReleaseError, "highest versionCode already uploaded: 42"):
            self.decide(41)
        # Even when 42 sits completed on the track (the previous release), a run that
        # did not upload it itself must not reuse it under a new versionName.
        tracks = {"internal": track("internal", (42, "completed"))}
        with self.assertRaisesRegex(ar.ReleaseError, "bump versionCode"):
            self.decide(42, tracks=tracks)

    def test_rerun_after_this_runs_upload_skips_the_upload(self):
        tracks = {"internal": track("internal", (42, "completed")), "alpha": track("alpha", (42, "completed"))}
        self.assertEqual(self.decide(42, tracks=tracks, selected=("internal", "alpha"), earlier=1), "skip")
        partial = {"internal": track("internal", (42, "completed")), "alpha": track("alpha", (41, "completed"))}
        with self.assertRaisesRegex(ar.ReleaseError, "alpha"):
            self.decide(42, tracks=partial, selected=("internal", "alpha"), earlier=1)
        draft = {"internal": track("internal", (42, "draft"))}
        with self.assertRaises(ar.ReleaseError):
            self.decide(42, tracks=draft, earlier=1)

    def test_known_version_codes_cover_bundles_apks_and_tracks(self):
        codes = ar.known_version_codes(
            {"bundles": [{"versionCode": 42}]},
            {"apks": [{"versionCode": 12}]},
            {"tracks": [track("production", (40, "completed"), (39, "halted"))]},
        )
        self.assertEqual(codes, {12, 39, 40, 42})
        self.assertEqual(ar.known_version_codes({}, {}, {}), set())

    def test_earlier_upload_attempt_reads_this_runs_history(self):
        jobs = {
            1: [{"name": "release", "steps": [{"name": "Upload to Play (internal)", "conclusion": "success"}]}],
            2: [{"name": "release", "steps": [{"name": "Upload to Play (internal)", "conclusion": "skipped"}]}],
        }
        self.assertEqual(ar.earlier_upload_attempt(jobs.get, 3), 1)
        self.assertIsNone(ar.earlier_upload_attempt(jobs.get, 1))
        failed = {1: [{"name": "release", "steps": [{"name": "Upload to Play (internal)", "conclusion": "failure"}]}]}
        self.assertIsNone(ar.earlier_upload_attempt(failed.get, 2))
        other_job = {1: [{"name": "quality / android", "steps": [{"name": "Upload to Play", "conclusion": "success"}]}]}
        self.assertIsNone(ar.earlier_upload_attempt(other_job.get, 2))

    def test_command_reads_play_without_committing(self):
        import argparse

        service = mock.MagicMock()
        edits = service.edits.return_value
        edits.insert.return_value.execute.return_value = {"id": "E1"}
        edits.bundles.return_value.list.return_value.execute.return_value = {"bundles": [{"versionCode": 42}]}
        edits.apks.return_value.list.return_value.execute.return_value = {}
        edits.tracks.return_value.list.return_value.execute.return_value = {"tracks": [track("internal", (42, "completed"))]}
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out, \
                mock.patch.object(ar, "play_publisher", return_value=service), \
                mock.patch.object(ar, "gradle_version", return_value=("7.3", 43)), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out.name, "GITHUB_RUN_ATTEMPT": "1"}), \
                redirect_stdout(io.StringIO()):
            ar.cmd_play_preflight(argparse.Namespace(tracks="internal"))
            self.assertIn("play_upload_needed=true", Path(out.name).read_text())
        edits.delete.assert_called_once_with(packageName=ar.PACKAGE_NAME, editId="E1")
        edits.commit.assert_not_called()


RELEASES = [  # the shape of `gh release list --json tagName,name`
    {"tagName": "android-v7.2", "name": "Android 7.2 (versionCode 42)"},
    {"tagName": "ios-v7.1.2", "name": "iOS 7.1.2 (build 39)"},
    {"tagName": "android-v7.1.1", "name": "Android 7.1.1 (versionCode 39)"},
    {"tagName": "android-v3.0.4", "name": "Android 3.0.4"},
    {"tagName": "v1.0.0", "name": "Fud AI (versionCode 99)"},
]


class GithubPreflightTests(unittest.TestCase):
    """A3: an APK on GitHub Releases never reuses an earlier Android versionCode."""

    def test_reads_android_release_titles_only(self):
        codes = ar.released_version_codes(RELEASES, "android-v7.3")
        self.assertEqual(codes, {42: "android-v7.2", 39: "android-v7.1.1"})

    def test_this_tags_own_release_is_ignored(self):
        # "Re-run all jobs" after the release was created must not fail on itself.
        self.assertEqual(ar.released_version_codes(RELEASES, "android-v7.2"), {39: "android-v7.1.1"})

    def test_version_code_must_be_higher_than_every_release(self):
        codes = ar.released_version_codes(RELEASES, "android-v7.3")
        ar.check_new_on_github(43, codes)
        ar.check_new_on_github(1, {})
        for code in (42, 41, 39):
            with self.subTest(code=code), self.assertRaisesRegex(ar.ReleaseError, "android-v7.2"):
                ar.check_new_on_github(code, codes)

    def test_command(self):
        import argparse
        import json as json_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "releases.json"
            path.write_text(json_module.dumps(RELEASES), encoding="utf-8")
            args = argparse.Namespace(releases=path)
            with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "android-v7.3"}), redirect_stdout(io.StringIO()):
                with mock.patch.object(ar, "gradle_version", return_value=("7.3", 43)):
                    ar.cmd_github_preflight(args)
                with mock.patch.object(ar, "gradle_version", return_value=("7.3", 42)), \
                        self.assertRaises(ar.ReleaseError):
                    ar.cmd_github_preflight(args)


def java_load(text):
    """Reference java.util.Properties.load() for the key=value lines the writer emits."""
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition("=")
        i = 0
        while i < len(rest) and rest[i] in " \t\f":  # unescaped leading whitespace is dropped
            i += 1
        out = []
        while i < len(rest):
            char = rest[i]
            if char == "\\":
                nxt = rest[i + 1]
                if nxt == "u":
                    out.append(chr(int(rest[i + 2:i + 6], 16)))
                    i += 6
                    continue
                out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(nxt, nxt))
                i += 2
                continue
            out.append(char)
            i += 1
        values[key] = "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")
    return values


class SigningPropertiesTests(unittest.TestCase):
    """P-19: signing secrets are written outside the checkout, escaped like Java expects."""

    TRICKY = ["plain", "back\\slash", " leading", "trailing ", "tab\tnew\nline", "a=b:c#d!e",
              "ünï 密码 🔑", "ends\\"]

    def test_values_round_trip_through_java_properties(self):
        for value in self.TRICKY:
            with self.subTest(value=value):
                escaped = ar.java_properties_escape(value)
                self.assertTrue(escaped.isascii())
                trailing = len(escaped) - len(escaped.rstrip("\\"))
                self.assertEqual(trailing % 2, 0, "an odd trailing backslash would continue the line")
                self.assertEqual(java_load(f"k={escaped}")["k"], value)

    def test_writes_all_four_entries_with_mode_600(self):
        env = {"ANDROID_KEYSTORE_PASSWORD": "s3cr\\t", "ANDROID_KEY_ALIAS": "fudai", "ANDROID_KEY_PASSWORD": " k p"}
        with tempfile.TemporaryDirectory() as tmp:
            import argparse

            out = Path(tmp) / "keystore.properties"
            with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
                ar.cmd_write_signing_properties(argparse.Namespace(keystore=Path("/tmp/fudai-release.jks"), out=out))
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertEqual(java_load(out.read_text(encoding="ascii")), {
                "storeFile": "/tmp/fudai-release.jks", "storePassword": "s3cr\\t",
                "keyAlias": "fudai", "keyPassword": " k p",
            })

    def test_missing_secret_fails(self):
        with self.assertRaisesRegex(ar.ReleaseError, "ANDROID_KEY_PASSWORD"):
            ar.signing_properties(Path("k.jks"), {"ANDROID_KEYSTORE_PASSWORD": "p", "ANDROID_KEY_ALIAS": "a"})


class PlayWhatsNewTests(unittest.TestCase):
    """The strict check the gates job runs only when ANDROID_UPLOAD_WHATS_NEW is on."""

    def write(self, block):
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                prepare_whats_new.write_play_whatsnew(block, Path(out.name))
            except SystemExit as exc:
                return exc.code, Path(out.name)
        return 0, Path(out.name)

    def test_each_locale_becomes_a_whatsnew_file(self):
        code, out = self.write("<en-US>\nFixes.\n</en-US>\n<de-DE>\nFehler.\n</de-DE>\n")
        self.assertEqual(code, 0)
        # The upload action sends the file verbatim: exactly the checked text.
        self.assertEqual((out / "whatsnew-en-US").read_text(encoding="utf-8"), "Fixes.")
        self.assertTrue((out / "whatsnew-de-DE").is_file())

    def test_the_500_character_limit_is_what_play_receives(self):
        text = "x" * 499 + "é"
        code, out = self.write(f"<en-US>\n{text}\n</en-US>")
        self.assertEqual(code, 0)
        sent = (out / "whatsnew-en-US").read_text(encoding="utf-8")
        self.assertEqual(sent, text)
        self.assertEqual(len(sent), 500)
        code, _ = self.write(f"<en-US>\n{text}y\n</en-US>")
        self.assertEqual(code, 1)

    def test_markdown_is_rejected(self):
        for body in ("**Bold** fixes", "See [notes](https://fud-ai.app)", "Run `x`"):
            with self.subTest(body=body):
                code, _ = self.write(f"<en-US>\n{body}\n</en-US>")
                self.assertEqual(code, 1)
        code, _ = self.write("<en-US>\n• Faster logging\n- Fixes\n</en-US>")
        self.assertEqual(code, 0)

    def test_missing_empty_or_too_long_text_fails(self):
        for label, block in {
            "no locales": "Fixes.",
            "empty": "<en-US>\n\n</en-US>",
            "too long": "<en-US>\n" + "x" * 501 + "\n</en-US>",
        }.items():
            with self.subTest(label):
                code, _ = self.write(block)
                self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
