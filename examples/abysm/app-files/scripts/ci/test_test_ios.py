#!/usr/bin/env python3
"""Offline tests of scripts/test-ios.sh: the simulator is shut down, and confirmed shut
down, between the unit and UI tests, against a fake xcrun (no simulator).

These are ABYSM's IosTestScriptTests, moved unchanged from scripts/store/test_ios_release.py
when its store tooling moved to store-release (examples/abysm in apoorvdarshan/store-release).
Run: python3 -m unittest -v scripts/ci/test_test_ios.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Stand-ins for `xcrun simctl` and `sleep` in IosTestScriptTests (no simulator).
# Each `simctl list devices --json` reports the next state from fake.json's
# "reads" (the last one repeats; null means simctl does not list the simulator),
# `simctl shutdown SIM` exits with "shutdown_exit", and every call is recorded.
FAKE_XCRUN = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path

state_file = Path(__file__).with_name("fake.json")
fake = json.loads(state_file.read_text())
args = sys.argv[1:]
fake["calls"].append(" ".join(args))
code = 0
if args == ["simctl", "list", "devices", "--json"]:
    state = fake["reads"].pop(0) if len(fake["reads"]) > 1 else fake["reads"][0]
    devices = [{"udid": "OTHER", "name": "A developer's iPhone", "state": "Booted"}]
    if state is not None:
        devices.append({"udid": "SIM", "name": "ABYSM CI", "state": state})
    print(json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": devices}}))
elif args == ["simctl", "shutdown", "SIM"]:
    code = fake["shutdown_exit"]
else:
    code = 64
state_file.write_text(json.dumps(fake))
sys.exit(code)
'''
FAKE_SLEEP = '#!/bin/sh\necho "$1" >> "$(dirname "$0")/sleeps"\n'


class IosTestScriptTests(unittest.TestCase):
    """scripts/test-ios.sh: the simulator is shut down, and confirmed shut down, between the suites."""

    SCRIPT = (ROOT / "scripts" / "test-ios.sh").read_text()

    def shut_down(self, reads: list[str | None], *, shutdown_exit: int = 0) -> tuple[subprocess.CompletedProcess,
                                                                                    list[str], list[str]]:
        """Run the script's shut_down_simulator() under its own shell options against the fakes."""
        functions = [re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", self.SCRIPT, re.M | re.S)
                     for name in ("simulator_state", "shut_down_simulator")]
        self.assertTrue(all(functions), "simulator_state() and shut_down_simulator() in scripts/test-ios.sh")
        with tempfile.TemporaryDirectory() as tmp:
            fakes = Path(tmp)
            (fakes / "fake.json").write_text(json.dumps({"reads": reads, "shutdown_exit": shutdown_exit, "calls": []}))
            for name, body in (("xcrun", FAKE_XCRUN), ("sleep", FAKE_SLEEP)):
                (fakes / name).write_text(body)
                (fakes / name).chmod(0o755)
            harness = "\n".join([
                re.search(r"^set -euo pipefail$", self.SCRIPT, re.M).group(0),
                "simulator_id=SIM",
                *(function.group(0) for function in functions),
                "if shut_down_simulator; then echo SHUT_DOWN; else echo NOT_SHUT_DOWN; fi",
            ])
            result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False,
                                    env=dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}"))
            calls = json.loads((fakes / "fake.json").read_text())["calls"]
            sleeps = (fakes / "sleeps").read_text().split() if (fakes / "sleeps").exists() else []
        return result, calls, sleeps

    def test_the_ui_tests_start_only_after_the_simulator_is_shut_down(self) -> None:
        self.assertIn("run_bundle AbysmTests UnitTests || status=1\n\nshut_down_simulator || exit 1\n\n"
                      "run_bundle AbysmUITests UITests || status=1\n", self.SCRIPT)
        # The old check listed the simulator's launchd jobs, which fails once the
        # simulator is shut down, and read that failure as "the app is gone".
        self.assertNotIn("launchctl", self.SCRIPT)

    def test_a_booted_or_shutting_down_simulator_is_waited_for_until_shut_down(self) -> None:
        # Read before the shutdown, then polled: still going down once, then shut down.
        for before, shutdown_exit in (("Booted", 0), ("Shutting Down", 149)):
            with self.subTest(before=before):
                result, calls, sleeps = self.shut_down([before, before, "Shutdown"], shutdown_exit=shutdown_exit)
                self.assertEqual((result.returncode, result.stdout.splitlines()[-1]), (0, "SHUT_DOWN"), result.stderr)
                self.assertIn(f"state after the unit tests: {before}", result.stdout)
                self.assertIn("simctl shutdown SIM", calls)
                self.assertEqual(sleeps, ["0.5"])

    def test_a_simulator_xcodebuild_already_shut_down_is_ready_at_once(self) -> None:
        # simctl refuses to shut down a shut-down simulator (exit 149); that is fine.
        result, calls, sleeps = self.shut_down(["Shutdown"], shutdown_exit=149)
        self.assertEqual((result.returncode, result.stdout.splitlines()[-1]), (0, "SHUT_DOWN"), result.stderr)
        self.assertIn("state after the unit tests: Shutdown", result.stdout)
        self.assertEqual(sleeps, [])

    def test_a_simulator_that_does_not_shut_down_stops_the_run(self) -> None:
        result, _, sleeps = self.shut_down(["Booted"])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "NOT_SHUT_DOWN"), result.stderr)
        self.assertIn("still Booted, not shut down; not starting the UI tests", result.stderr)
        self.assertEqual(len(sleeps), 60)

    def test_a_simulator_simctl_does_not_list_stops_the_run(self) -> None:
        result, calls, _ = self.shut_down([None])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "NOT_SHUT_DOWN"), result.stderr)
        self.assertIn("simctl lists 0 simulators with id SIM", result.stderr)
        self.assertNotIn("simctl shutdown SIM", calls)


if __name__ == "__main__":
    unittest.main()
