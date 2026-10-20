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
# The third-party actions the reusable workflows and job-setup may use. Their pins are not listed
# here: Dependabot bumps them (one grouped PR moves every file), and test_pins keeps them consistent.
RELEASE_ACTIONS = {"actions/checkout", "actions/setup-python", "actions/cache", "actions/upload-artifact",
                   "actions/setup-java", "gradle/actions/setup-gradle", "sigstore/cosign-installer",
                   "actions/attest-build-provenance", "r0adkll/upload-google-play",
                   # v0.2.0: the source archive hand-off (Scowld) and the Android SDK packages (Verceltics).
                   "actions/download-artifact", "android-actions/setup-android"}
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
    "ios-source-archive.yml": ("source-archive", "ubuntu-latest", 10,
                               {"contents": "read", "id-token": "write", "attestations": "write"},
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
                            {"config": False, "keystore_only": False, **{name: True for name in (
                                "gates", "mode", "build", "github_release", "listing", "whats_new", "screenshots",
                                "production", "play_upload", "play_tracks", "tool_sha")}},
                            # The key alias and password: only keystore-properties signing uses them.
                            {"ANDROID_KEYSTORE_BASE64": True, "ANDROID_KEYSTORE_PASSWORD": True,
                             "ANDROID_KEY_ALIAS": False, "ANDROID_KEY_PASSWORD": False,
                             "PLAY_SERVICE_ACCOUNT_JSON": False}, []),
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


PIN = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")


def collect_pins(files: list[tuple[str, str]]) -> tuple[dict[str, set[tuple[str, str]]], list[str]]:
    """({action: {(sha, version)}}, malformed pins) over (file name, text) pairs; local and
    store-release refs are skipped (the probe pins this repository's own commit)."""
    pins: dict[str, set[tuple[str, str]]] = {}
    malformed: list[str] = []
    for name, text in files:
        for number, line in enumerate(text.splitlines(), 1):
            match = PIN.match(line)
            if not match:
                continue
            target, rest = match.groups()
            if target.startswith("./") or target.startswith("apoorvdarshan/store-release/"):
                continue
            action, _, ref = target.partition("@")
            comment = rest.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", ref) or not re.fullmatch(r"# v\d+\.\d+\.\d+", comment):
                malformed.append(f"{name} line {number}: {target}{rest}")
                continue
            pins.setdefault(action, set()).add((ref, comment[2:]))
    return pins, malformed


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
        """Every third-party action is pinned to a full commit SHA with a # vX.Y.Z comment, and each action
        to the same SHA and version in every file, so a half-applied bump fails while a Dependabot bump
        (which moves every file at once) passes."""
        files = [(path.name, path.read_text()) for path in
                 sorted(WORKFLOWS.glob("*.yml")) + [TOOL_ROOT / ".github/actions/job-setup/action.yml"]]
        pins, malformed = collect_pins(files)
        self.assertEqual(malformed, [])
        for action, found in sorted(pins.items()):
            with self.subTest(action=action):
                self.assertEqual(len(found), 1, f"{action} is pinned to {sorted(found)}; use one pin everywhere")
        release, _ = collect_pins([(name, text) for name, text in files if name in INTERFACES or name == "action.yml"])
        self.assertEqual(set(release), RELEASE_ACTIONS, "a new action in the release path needs review here")

    def test_the_pin_rule(self) -> None:
        old, new = "3" * 40, "1" * 40
        bumped = [("a.yml", f"      - uses: actions/checkout@{new} # v7.0.2\n"),
                  ("b.yml", f"      - uses: actions/checkout@{new} # v7.0.2\n")]
        self.assertEqual(collect_pins(bumped), ({"actions/checkout": {(new, "v7.0.2")}}, []))
        half = [bumped[0], ("b.yml", f"      - uses: actions/checkout@{old} # v7.0.1\n")]
        self.assertEqual(collect_pins(half)[0], {"actions/checkout": {(new, "v7.0.2"), (old, "v7.0.1")}})
        loose = [("c.yml", "    uses: actions/checkout@v7\n    uses: actions/setup-python@" + new + "\n"
                           "    uses: ./.github/actions/job-setup\n"
                           "    uses: apoorvdarshan/store-release/.github/workflows/x.yml@" + old + " # probe\n")]
        self.assertEqual(collect_pins(loose), ({}, ["c.yml line 1: actions/checkout@v7",
                                                    f"c.yml line 2: actions/setup-python@{new}"]))

    def test_every_job_declares_its_permissions(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for job_id, job in load(path.name)["jobs"].items():
                with self.subTest(workflow=path.name, job=job_id):
                    self.assertIn("permissions", job, "explicit permissions on every job")

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
        # Two per flow: the fud-ai flow's (plaintext key) and the verceltics flow's (key file).
        self.assertEqual(len(uploads), 4)
        for step in job["steps"]:
            if step.get("name", "").startswith("Upload to Play"):
                flow = "!=" if "ported flow" in step["name"] else "=="
                self.assertIn(f"env.STORE_RELEASE_ANDROID_FLOW {flow} 'fud-ai'", step["if"])
        for step in job["steps"]:
            if step.get("uses", "").startswith("r0adkll/upload-google-play"):
                self.assertIn("inputs.play_tracks != ''", step["if"])
                self.assertNotIn("releaseName", step["with"])

    def test_what_can_fail_runs_before_anything_is_built(self) -> None:
        def steps(name: str) -> list[dict]:
            return only_job(load(name))[1]["steps"]

        def index(found: list[dict], name: str) -> int:
            return [step.get("name") for step in found].index(name)

        # The config's pins on every iOS prepare (fud-ai's unit tests checked these facts there).
        prepare = steps("ios-prepare.yml")
        pinned = prepare[index(prepare, "Check the app files the config pins")]
        self.assertNotIn("if", pinned)
        self.assertEqual(pinned["run"], "python3 -I -u .store-release/run.py --app-root app config check --pinned")
        self.assertLess(index(prepare, "Check the app files the config pins"), index(prepare, "Resolve iOS release switches"))
        self.assertIn("config check --files", "\n".join(step.get("run", "") for step in steps("checks.yml")))
        # STORE_SYNC_REVENUECAT on every Android tag run, before anything is built; the gate stays in the release job.
        gates = steps("android-gates.yml")
        switch = gates[index(gates, "Check the RevenueCat switch")]
        self.assertEqual(switch["if"], "github.event_name == 'push' && env.STORE_RELEASE_ANDROID_FLOW == 'fud-ai'")
        self.assertEqual(switch["env"], {"STORE_SYNC_REVENUECAT": "${{ vars.STORE_SYNC_REVENUECAT }}"})
        self.assertIn("revenuecat-gate --check-switch", switch["run"])
        publish = steps("android-publish.yml")
        gate = publish[index(publish, "RevenueCat catalog sync (gated)")]
        self.assertEqual(gate["run"], "python3 -I -u .store-release/run.py --app-root app revenuecat-gate")
        self.assertLess(index(publish, "Play listing / screenshots upload"), index(publish, "RevenueCat catalog sync (gated)"))

    def test_the_plans_name_store_releases_own_commit(self) -> None:
        for name in ("ios-store-plan.yml", "android-store-plan.yml"):
            plan = [step for step in only_job(load(name))[1]["steps"] if step.get("name", "").startswith("Plan what")]
            with self.subTest(workflow=name):
                self.assertEqual(len(plan), 1)
                self.assertEqual(plan[0]["env"]["TOOL_SHA"], "${{ job.workflow_sha }}")

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
