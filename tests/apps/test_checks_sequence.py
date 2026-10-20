"""checks.yml's own run steps, in order, against fixture apps: both platforms (Fud AI's shape) and an
iOS-only app. Each step's `if:` is evaluated from what `config export --job checks` writes, so a
single-platform app skips exactly the other platform's steps and everything else passes."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from support import apps as support_apps
from support import clirun, trees

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = support_apps.TOOL_ROOT
CHECKS = TOOL_ROOT / ".github" / "workflows" / "checks.yml"
# Run elsewhere: the network (pip, the release tag, the yanked list) or store-release's own suites.
NOT_RUN_HERE = {
    "Test the store release tooling (offline, stdlib only)",
    "Install and import the store tooling (no store calls)",
    "Test screenshot staging with Pillow (offline)",
    "Check the callers and their pins",
    "Refuse a yanked store-release version",
}
CONDITION = re.compile(r"env\.(STORE_RELEASE_HAS_IOS|STORE_RELEASE_HAS_ANDROID) == 'true'")


def ios_only(text: str) -> str:
    head, sep, _ = text.partition("\n[android]\n")
    assert sep, "the config has an [android] table"
    return head + "\n"


class ChecksSequenceTests(unittest.TestCase):
    def run_sequence(self, root: Path) -> tuple[list[str], list[str]]:
        """(steps that ran, steps skipped by their if:); fails on the first step that fails."""
        steps = yaml.safe_load(CHECKS.read_text())["jobs"]["checks"]["steps"]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / ".store-release").symlink_to(TOOL_ROOT)
            (work / "app").symlink_to(root)
            (work / "bin").mkdir()
            # The steps call python3: this interpreter (a wrapper keeps a virtual environment's packages).
            wrapper = work / "bin" / "python3"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            wrapper.chmod(0o755)
            (work / "runner-temp").mkdir()
            github_env = work / "github.env"
            github_env.write_text("")
            base = clirun.clean_env(PATH=f"{work / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                                    RUNNER_TEMP=str(work / "runner-temp"), GITHUB_ENV=str(github_env))
            # job-setup: config check, then config export (the sparse checkout is the fixture itself).
            for args in (("config", "check"), ("config", "export", "--job", "checks")):
                result = subprocess.run([sys.executable, "-I", "-u", str(TOOL_ROOT / "run.py"), "--app-root", "app",
                                         *args], cwd=work, env=base, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            exported = clirun.outputs(github_env)
            ran, skipped = [], []
            for step in steps:
                name = step.get("name", step.get("uses", "?"))
                if "uses" in step or name in NOT_RUN_HERE:
                    continue
                condition = step.get("if")
                if condition is not None:
                    match = CONDITION.fullmatch(condition)
                    self.assertIsNotNone(match, f"{name}: this test only knows the platform conditions, not {condition!r}")
                    if exported.get(match.group(1)) != "true":
                        skipped.append(name)
                        continue
                env = {**base, **exported}
                for key, value in (step.get("env") or {}).items():
                    env[key] = "" if "${{" in str(value) else str(value)
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=work, env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, f"{name} failed:\n{result.stdout}\n{result.stderr}")
                ran.append(name)
            return ran, skipped

    def test_both_platforms_run_every_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = trees.write_fud_ai_tree(Path(tmp) / "fud-ai", screenshots=True)
            ran, skipped = self.run_sequence(root)
        self.assertEqual(skipped, [])
        self.assertIn("Validate the Play publisher step (dry-run, no network)", ran)
        self.assertIn("Check the project version and export compliance", ran)

    def test_an_ios_only_app_skips_the_android_steps_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = ios_only((TOOL_ROOT / "examples" / "example" / "store-release.toml").read_text())
            root = trees.write_example_tree(Path(tmp) / "ios-only", config=text)
            ran, skipped = self.run_sequence(root)
        self.assertEqual(skipped, ["Validate the Play publisher step (dry-run, no network)", "Check the gradle version"])
        for name in ("Check every file the config names", "Validate the ASC store steps (dry-run, no network)",
                     "Check the project version and export compliance",
                     "Stage every screenshot set for upload (Pillow, no store APIs)"):
            self.assertIn(name, ran)


if __name__ == "__main__":
    unittest.main()
