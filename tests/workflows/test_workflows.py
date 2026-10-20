"""The ten reusable workflows and the job-setup action: interfaces, permissions, pins and shell hygiene (PyYAML)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

TOOL_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = TOOL_ROOT / ".github" / "workflows"
ASC = ("APP_STORE_CONNECT_API_KEY_ID", "APP_STORE_CONNECT_ISSUER_ID", "APP_STORE_CONNECT_API_KEY_P8")
KEYSTORE = ("ANDROID_KEYSTORE_BASE64", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD")
PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "actions/cache": ("55cc8345863c7cc4c66a329aec7e433d2d1c52a9", "v6.1.0"),
    "actions/upload-artifact": ("cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2"),
    "actions/setup-java": ("b843ebf9b913146b38da2eaa7aeddf76074ae1ad", "v6.1.0"),
    "gradle/actions/setup-gradle": ("3f5f9adaf7d9fecd50b5935e54106014257a94e6", "v6.4.0"),
    "sigstore/cosign-installer": ("6f9f17788090df1f26f669e9d70d6ae9567deba6", "v4.1.2"),
    "actions/attest-build-provenance": ("4d101475d8b20a2381f78447822ac1eab6504dd8", "v4.2.2"),
    "r0adkll/upload-google-play": ("e738b9dd8f2476ea806d921b64aacd24f34515a5", "v1.1.5"),
}
SCALAR_OUTPUTS = {"mode", "create_version", "build", "upload_build", "listing", "whats_new", "screenshots", "creative",
                  "submit", "public_release", "asc", "poll_seconds", "wait_minutes", "marketing_version",
                  "build_number", "tool_sha", "upload_started_at", "build_id", "github_release", "production",
                  "play_upload", "play_tracks"}

# file: (job id, runner, timeout, permissions, inputs {name: required}, secrets {name: required}, outputs)
INTERFACES = {
    "ios-prepare.yml": ("prepare", "ubuntu-latest", 15, {"contents": "read"},
                        {"config": False, "marketing_version": False}, {name: False for name in ASC},
                        ["mode", "create_version", "build", "upload_build", "listing", "whats_new", "screenshots",
                         "creative", "submit", "public_release", "asc", "poll_seconds", "wait_minutes",
                         "marketing_version", "build_number", "tool_sha"]),
    "ios-build.yml": ("build", "${{ inputs.runner }}", 90, {"contents": "read"},
                      {"config": False, "runner": False, "gates": True, "mode": True, "marketing_version": True,
                       "build_number": True, "tool_sha": True},
                      {**{name: True for name in ASC}, "IOS_DEV_SIGNING_P12_BASE64": True,
                       "IOS_DEV_SIGNING_P12_PASSWORD": True}, ["upload_started_at"]),
    "ios-wait-for-build.yml": ("wait-for-build", "ubuntu-latest", 310, {"contents": "read"},
                               {"config": False, "marketing_version": True, "build_number": True,
                                "upload_started_at": True, "wait_minutes": True, "poll_seconds": True,
                                "tool_sha": True}, {name: True for name in ASC}, ["build_id"]),
    "ios-app-store-connect.yml": ("app-store-connect", "ubuntu-latest", 60, {"contents": "read"},
                                  {"config": False, **{name: True for name in (
                                      "gates", "marketing_version", "build_number", "build_id", "upload_build",
                                      "listing", "whats_new", "screenshots", "creative", "submit", "public_release",
                                      "tool_sha")}}, {name: True for name in ASC}, []),
    "ios-github-release.yml": ("github-release", "ubuntu-latest", 10, {"contents": "write"},
                               {"config": False, "gates": True, "marketing_version": True, "uploaded_build": True,
                                "tool_sha": True}, {}, []),
    "ios-store-plan.yml": ("store-plan", "ubuntu-latest", 15, {"contents": "read"},
                           {"config": False, "marketing_version": True, "tool_sha": True},
                           {name: True for name in ASC}, []),
    "android-gates.yml": ("gates", "ubuntu-latest", 10, {"contents": "read", "actions": "read"}, {"config": False},
                          {"PLAY_SERVICE_ACCOUNT_JSON": False},
                          ["mode", "build", "github_release", "listing", "whats_new", "screenshots", "production",
                           "play_upload", "play_tracks", "tool_sha"]),
    "android-publish.yml": ("release", "ubuntu-latest", 60,
                            {"contents": "write", "id-token": "write", "attestations": "write", "actions": "read"},
                            {"config": False, **{name: True for name in (
                                "gates", "mode", "build", "github_release", "listing", "whats_new", "screenshots",
                                "production", "play_upload", "play_tracks", "tool_sha")}},
                            {**{name: True for name in KEYSTORE}, "PLAY_SERVICE_ACCOUNT_JSON": False}, []),
    "android-store-plan.yml": ("store-plan", "ubuntu-latest", 15, {"contents": "read"},
                               {"config": False, "tool_sha": True}, {"PLAY_SERVICE_ACCOUNT_JSON": False}, []),
    "checks.yml": ("checks", "ubuntu-latest", 15, {"contents": "read"}, {"config": False}, {}, []),
}
FIRST_JOBS = {"ios-prepare.yml", "android-gates.yml", "checks.yml"}
SETUP_JOBS = {"ios-prepare.yml": "ios-prepare", "ios-build.yml": "ios-build", "ios-wait-for-build.yml": "ios-wait-for-build",
              "ios-app-store-connect.yml": "ios-app-store-connect", "ios-github-release.yml": "ios-github-release",
              "ios-store-plan.yml": "ios-store-plan", "android-gates.yml": "android-gates",
              "android-publish.yml": "android-publish", "android-store-plan.yml": "android-store-plan",
              "checks.yml": "checks"}


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True)
    return data


def only_job(data: dict) -> tuple[str, dict]:
    (job_id, job), = data["jobs"].items()
    return job_id, job


class ReusableWorkflowTests(unittest.TestCase):
    def test_the_ten_files(self) -> None:
        reusable = sorted(p.name for p in WORKFLOWS.glob("*.yml") if "workflow_call" in load(p.name)["on"]
                          and not p.name.startswith("probe"))
        self.assertEqual(reusable, sorted(INTERFACES))

    def test_interfaces_permissions_and_timeouts(self) -> None:
        for name, (job_id, runner, timeout, permissions, inputs, secrets, outputs) in INTERFACES.items():
            with self.subTest(workflow=name):
                data = load(name)
                self.assertEqual(list(data["on"]), ["workflow_call"])
                call = data["on"]["workflow_call"]
                self.assertEqual({key: bool(value.get("required", False)) for key, value in (call.get("inputs") or {}).items()},
                                 inputs)
                self.assertEqual(call["inputs"]["config"]["default"], "store/store-release.toml")
                self.assertEqual({key: value["required"] for key, value in (call.get("secrets") or {}).items()}, secrets)
                self.assertEqual(list(call.get("outputs") or {}), outputs)
                for output, spec in (call.get("outputs") or {}).items():
                    self.assertEqual(spec["value"], f"${{{{ jobs.{job_id}.outputs.{output} }}}}")
                self.assertEqual(data["permissions"], {"contents": "read"})
                self.assertEqual(data["defaults"], {"run": {"shell": "bash"}})
                self.assertNotIn("concurrency", data)
                found_id, job = only_job(data)
                self.assertEqual(found_id, job_id)
                self.assertEqual(job["runs-on"], runner)
                self.assertEqual(job["timeout-minutes"], timeout)
                self.assertEqual(job["permissions"], permissions)
                self.assertNotIn("concurrency", job)
                self.assertTrue(set(job.get("outputs") or {}) <= SCALAR_OUTPUTS)
                self.assertEqual(sorted(job.get("outputs") or {}), sorted(outputs))
                for value in (job.get("env") or {}).values():
                    self.assertNotIn("secrets.", str(value), "secrets live in step env, never at job level")
        self.assertGreater(INTERFACES["ios-wait-for-build.yml"][2], 300)

    def test_every_job_starts_with_the_setup_sequence(self) -> None:
        for name, job_name in SETUP_JOBS.items():
            with self.subTest(workflow=name):
                steps = only_job(load(name))[1]["steps"]
                start = next(i for i, step in enumerate(steps) if step.get("name") == "Check out the app's store-release config")
                for step in steps[:start]:
                    self.assertIn("secrets", step["name"].lower(), "only a secret presence check comes before setup")
                    self.assertNotIn("python3", step.get("run", ""))
                app, tool, setup = steps[start:start + 3]
                self.assertEqual(app["with"], {"path": "app", "persist-credentials": False, "filter": "blob:none",
                                               "sparse-checkout-cone-mode": False,
                                               "sparse-checkout": "/${{ inputs.config }}"})
                self.assertEqual(tool["with"], {"repository": "${{ job.workflow_repository }}",
                                                "ref": "${{ job.workflow_sha }}", "path": ".store-release",
                                                "persist-credentials": False})
                self.assertEqual(setup["uses"], "./.store-release/.github/actions/job-setup")
                expected = {"job": job_name, "config": "${{ inputs.config }}", "tool-sha": "${{ job.workflow_sha }}"}
                if name not in FIRST_JOBS:
                    expected["expected-tool-sha"] = "${{ inputs.tool_sha }}"
                self.assertEqual(setup["with"], expected)

    def test_pins(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")) + [TOOL_ROOT / ".github/actions/job-setup/action.yml"]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                match = re.match(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", line)
                if not match:
                    continue
                target, rest = match.groups()
                with self.subTest(file=path.name, line=number):
                    if target.startswith("./") or target.startswith("apoorvdarshan/store-release/"):
                        continue
                    action, _, ref = target.partition("@")
                    self.assertRegex(ref, r"^[0-9a-f]{40}$")
                    self.assertRegex(rest.strip(), r"^# v\d+\.\d+\.\d+$")
                    if path.name in INTERFACES or path.name == "action.yml":
                        self.assertEqual((ref, rest.strip()[2:]), PINS[action])

    def run_scripts(self, name: str) -> list[tuple[str, str]]:
        return [(step.get("name", "?"), step["run"]) for step in only_job(load(name))[1]["steps"] if "run" in step]

    def test_shell_hygiene(self) -> None:
        for name in INTERFACES:
            for step, script in self.run_scripts(name):
                with self.subTest(workflow=name, step=step):
                    self.assertNotIn("${{", script, "expressions reach shell only through env:")
                    for match in re.finditer(r"python3(\s+\S+){0,3}", script):
                        invocation = match.group(0)
                        self.assertTrue(invocation.startswith(("python3 -I -u .store-release/run.py",
                                                               "python3 -m pip install", "python3 -m pip check",
                                                               "python3 -I -c")), invocation)
                    for install in re.findall(r"python3 -m pip install[^\n]*(?:\\\n[^\n]*)*", script):
                        self.assertIn("--require-hashes", install)
                    for command in ("screenshots stage", "listing prepare", "whats-new prepare"):
                        for call in re.findall(rf"{command}[^\n]*", script):
                            self.assertRegex(call, r'--out "\$RUNNER_TEMP/')

    def test_secrets_never_inherit(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            self.assertNotIn("secrets: inherit", path.read_text(), path.name)

    def test_the_android_release_job_contract(self) -> None:
        job_id, job = only_job(load("android-publish.yml"))
        self.assertEqual(job_id, "release")
        uploads = [step["name"] for step in job["steps"] if step.get("name", "").startswith("Upload to Play")]
        self.assertEqual(len(uploads), 2)
        for step in job["steps"]:
            if step.get("uses", "").startswith("r0adkll/upload-google-play"):
                self.assertIn("inputs.play_tracks != ''", step["if"])
                self.assertNotIn("releaseName", step["with"])

    def test_the_build_job_never_installs_the_lock(self) -> None:
        for _, script in self.run_scripts("ios-build.yml"):
            self.assertNotIn("pip install", script)

    def test_job_setup_action(self) -> None:
        action = yaml.safe_load((TOOL_ROOT / ".github/actions/job-setup/action.yml").read_text())
        self.assertEqual(sorted(action["inputs"]), ["config", "expected-tool-sha", "job", "tool-sha"])
        self.assertEqual(action["runs"]["using"], "composite")
        script = action["runs"]["steps"][1]["run"]
        self.assertNotIn("${{", script)
        for needle in ("tool check --tool-sha", "config check", "paths --job", "sparse-checkout set --no-cone --stdin",
                       "config export --job"):
            self.assertIn(needle, script)


if __name__ == "__main__":
    unittest.main()
