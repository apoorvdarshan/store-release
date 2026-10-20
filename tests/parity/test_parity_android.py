"""Parity case 11: Android gradle parsing, signing properties, artifact verification, Play and GitHub preflights."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from store_release.android import release as android_release
from support import apps as support_apps
from support.upstream import ParityCase, outcome

support_apps.configure_fud_ai_defaults()

UPLOAD_DER = b"upload-certificate"
UPLOAD = hashlib.sha256(UPLOAD_DER).hexdigest()


def pem(der: bytes) -> str:
    return f"-----BEGIN CERTIFICATE-----\n{base64.b64encode(der).decode()}\n-----END CERTIFICATE-----\n"


def d16(result: tuple) -> tuple:
    """Allowlisted D16: the package names the config key where fud-ai named its constant."""
    return tuple(item.replace("android.upload_cert_sha256", "UPLOAD_CERT_SHA256") if isinstance(item, str) else item
                 for item in result)


def track(name, *releases):
    return {"track": name, "releases": [{"versionCodes": [str(code)], "status": status} for code, status in releases]}


class AndroidParityTests(ParityCase):
    def both(self, name: str, *args, **kwargs):
        return (outcome(lambda: getattr(self.up.android_release, name)(*args, **kwargs)),
                outcome(lambda: getattr(android_release, name)(*args, **kwargs)))

    def assert_same(self, name: str, *args, **kwargs) -> None:
        upstream, package = self.both(name, *args, **kwargs)
        self.assertEqual(package, upstream)

    def test_gradle_version(self) -> None:
        texts = ['versionCode = 42\nversionName = "7.2"\n', '  versionCode = 42  \n  versionName = "7.2"\n',
                 'versionCode = 1\nversionName = "1.0"\nversionName = "1.1"\n', 'versionName = "1.0"\n', "",
                 (self.app.root / "android/app/build.gradle.kts").read_text()]
        for text in texts:
            with self.subTest(text=text):
                self.assert_same("gradle_version", text)

    def test_signing_properties(self) -> None:
        envs = [{"ANDROID_KEYSTORE_PASSWORD": "s3cr\\t", "ANDROID_KEY_ALIAS": "fudai", "ANDROID_KEY_PASSWORD": " k p"},
                {"ANDROID_KEYSTORE_PASSWORD": "ünï 密码 🔑", "ANDROID_KEY_ALIAS": "a=b:c#d!e", "ANDROID_KEY_PASSWORD": "x\n"},
                {"ANDROID_KEYSTORE_PASSWORD": "p", "ANDROID_KEY_ALIAS": "a"}]
        for env in envs:
            with self.subTest(env=sorted(env)):
                self.assert_same("signing_properties", Path("/tmp/fudai-release.jks"), env)
        for value in ("plain", " leading", "ends\\", "tab\tnew\nline"):
            self.assert_same("java_properties_escape", value)

    def test_verify_with_fake_tools(self) -> None:
        badging = ("package: name='com.apoorvdarshan.calorietracker' versionCode='42' versionName='7.2' "
                   "platformBuildVersionName='16'\n")
        manifest = ('<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
                    'package="com.apoorvdarshan.calorietracker" android:versionCode="42" android:versionName="7.2"/>')
        signers = f"Signer #1 certificate SHA-256 digest: {UPLOAD}\n"
        aab = self.tmp / "app.aab"
        import zipfile

        with zipfile.ZipFile(aab, "w") as bundle:
            bundle.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n\r\nName: base/x\r\nSHA-256-Digest: A\r\n\r\n")
            bundle.writestr("META-INF/FUDAI.SF", "s")
            bundle.writestr("base/x", "data")

        def tools(keytool_cert: bytes, apk_signers: str, jar_ok: bool):
            def run(cmd, *, check=True):
                name = Path(cmd[0]).name
                if name == "keytool" and "-exportcert" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, pem(keytool_cert), "")
                if name == "aapt2":
                    return subprocess.CompletedProcess(cmd, 0, badging, "")
                if name == "apksigner":
                    return subprocess.CompletedProcess(cmd, 0, apk_signers, "")
                if name == "java":
                    return subprocess.CompletedProcess(cmd, 0, manifest, "")
                if name == "jarsigner":
                    return subprocess.CompletedProcess(cmd, 0 if jar_ok else 1, "jar verified." if jar_ok else "no", "")
                if name == "keytool":
                    return subprocess.CompletedProcess(cmd, 0, pem(keytool_cert), "")
                raise AssertionError(cmd)
            return run

        for keytool_cert, apk_signers, jar_ok in ((UPLOAD_DER, signers, True), (b"other-key", signers, True),
                                                  (UPLOAD_DER, "", True), (UPLOAD_DER, signers, False)):
            args = argparse.Namespace(aab=aab, apk=Path("app.apk"), keystore=Path("k.jks"), alias="fudai",
                                      bundletool=Path("bundletool.jar"), package="com.apoorvdarshan.calorietracker",
                                      expected_cert=UPLOAD)
            runs = []
            for module in (self.up.android_release, android_release):
                with mock.patch.object(module, "run", side_effect=tools(keytool_cert, apk_signers, jar_ok)), \
                        mock.patch.object(module, "build_tool", side_effect=lambda name: name):
                    runs.append(outcome(lambda: module.cmd_verify(args)))
            with self.subTest(cert=keytool_cert, signers=bool(apk_signers), jar_ok=jar_ok):
                self.assertEqual(d16(runs[1]), runs[0])

    def test_play_decisions_tracks_and_github(self) -> None:
        tracks = {"internal": track("internal", (42, "completed")), "alpha": track("alpha", (41, "completed"))}
        for code, known, selected, earlier in ((43, {40, 41, 42}, ["internal"], None), (42, {42}, ["internal"], None),
                                               (42, {42}, ["internal"], 1), (42, {42}, ["internal", "alpha"], 1),
                                               (1, set(), ["production"], None), (41, {42}, ["internal"], 2)):
            with self.subTest(code=code, selected=selected, earlier=earlier):
                self.assert_same("play_upload_decision", code, known_codes=known, tracks=tracks, selected=selected,
                                 earlier_upload_attempt=earlier)
        for raw in ("internal,alpha", "production", "", "beta", "internal,internal", "internal,Production"):
            self.assert_same("parse_tracks", raw)
        fetch = {"internal": {"releases": [{"versionCodes": ["42"], "status": "completed"}]},
                 "production": {"releases": [{"versionCodes": ["42"], "status": "halted"}]}}
        for selected in (["internal"], ["production"], ["internal", "production"]):
            self.assert_same("check_tracks", fetch.get, selected, 42)
        self.assert_same("known_version_codes", {"bundles": [{"versionCode": 42}]}, {"apks": [{"versionCode": 12}]},
                         {"tracks": [track("production", (40, "completed"))]})
        jobs = {1: [{"name": "release", "steps": [{"name": "Upload to Play (internal)", "conclusion": "success"}]}],
                2: [{"name": "quality / android", "steps": [{"name": "Upload to Play", "conclusion": "success"}]}]}
        self.assert_same("earlier_upload_attempt", jobs.get, 3)
        self.assert_same("earlier_upload_attempt", {1: jobs[2]}.get, 2)
        releases = [{"tagName": "android-v7.2", "name": "Android 7.2 (versionCode 42)"},
                    {"tagName": "ios-v7.1.2", "name": "iOS 7.1.2 (build 39)"},
                    {"tagName": "android-v7.1.1", "name": "Android 7.1.1 (versionCode 39)"},
                    {"tagName": "android-v3.0.4", "name": "Android 3.0.4"}]
        for current in ("android-v7.3", "android-v7.2"):
            self.assert_same("released_version_codes", releases, current)
        for code in (43, 42, 39, 1):
            self.assert_same("check_new_on_github", code, {42: "android-v7.2", 39: "android-v7.1.1"})
        path = self.tmp / "releases.json"
        path.write_text(json.dumps(releases))
        for version in (("7.3", 43), ("7.3", 42)):
            runs = []
            for module in (self.up.android_release, android_release):
                with mock.patch.object(module, "gradle_version", return_value=version), \
                        mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "android-v7.3"}):
                    runs.append(outcome(lambda: module.cmd_github_preflight(argparse.Namespace(releases=path))))
            self.assertEqual(runs[1], runs[0])

    def test_gates_command_output(self) -> None:
        for env in ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "android-v7.2", "ANDROID_RELEASE_AUTOMATION": "true",
                     "ANDROID_PRODUCTION_ROLLOUT": "true", "ANDROID_UPLOAD_WHATS_NEW": "true"},
                    {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "android-v7.2"},
                    {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_NAME": "main"},
                    {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "android-v9.9"}):
            runs = []
            for module in (self.up.android_release, android_release):
                with mock.patch.dict(os.environ, env, clear=True):
                    runs.append(outcome(lambda: module.main(["gates"])))
            with self.subTest(env=env):
                self.assertEqual(runs[1], runs[0])


if __name__ == "__main__":
    unittest.main()
