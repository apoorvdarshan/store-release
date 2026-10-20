"""Each app's own store test suite, unchanged, against store-release's port of that app's flow.

tests/flows/run_upstream.py runs the app's verbatim test files (tests/parity/upstream_<package>_*/)
in a fresh interpreter, with the flow modules standing in for the app's scripts and a synthetic
app tree as ROOT. Every test runs except the ones tests/flows/<package>.inventory lists, each with
the reason it stays with the app or tests a helper store-release replaces. The totals are pinned:
a test that disappears from a run, or an inventory line naming no test, fails here.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[2]
RUNNER = TOOL_ROOT / "tests" / "flows" / "run_upstream.py"

# app -> (upstream tests, left out by its inventory)
EXPECTED = {
    "scowld": (185, 16),
    "verceltics": (213, 4),
    "abysm": (215, 42),
    "pcm-notes": (218, 23),
    "quit-all": (170, 16),
}


class UpstreamSuiteTests(unittest.TestCase):
    # The app suites stage RGB screenshot copies with Pillow and mint ES256 tokens with PyJWT.
    needs_pillow = True

    def run_app(self, app: str) -> dict:
        with tempfile.TemporaryDirectory(prefix=f"flow-{app}-") as tmp:
            result = subprocess.run([sys.executable, "-I", str(RUNNER), app, tmp], capture_output=True, text=True,
                                    timeout=600)
        lines = result.stdout.strip().splitlines()
        self.assertTrue(lines, f"{app}: the runner printed nothing:\n{result.stderr[-3000:]}")
        return json.loads(lines[-1])

    def check(self, app: str) -> None:
        try:
            import PIL  # noqa: F401, PLC0415
            import jwt  # noqa: F401, PLC0415
        except ImportError:
            self.skipTest("needs Pillow and PyJWT (the store lock)")
        summary = self.run_app(app)
        total, left_out = EXPECTED[app]
        self.assertEqual(summary["unknown_inventory"], [], f"{app}: inventory lines that name no upstream test")
        self.assertEqual(summary["total"], total, f"{app}: the upstream suite changed size")
        self.assertEqual(summary["left_out"], left_out, f"{app}: the inventory changed")
        self.assertEqual(summary["run"], total - left_out)
        self.assertEqual(summary["failures"], [])
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["skipped"], [], f"{app}: an upstream test skipped")

    def test_scowld(self) -> None:
        self.check("scowld")

    def test_verceltics(self) -> None:
        self.check("verceltics")

    def test_abysm(self) -> None:
        self.check("abysm")

    def test_pcm_notes(self) -> None:
        self.check("pcm-notes")

    def test_quit_all(self) -> None:
        self.check("quit-all")


if __name__ == "__main__":
    unittest.main()
