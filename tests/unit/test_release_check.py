"""scripts/release_check.py: store-release's own release preconditions (release.yml). Stdlib only."""
from __future__ import annotations

import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_check", TOOL_ROOT / "scripts" / "release_check.py")
release_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_check)

SHA = "0123456789abcdef0123456789abcdef01234567"
ENV = {"GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "owner/store-release"}
CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-02

Impact: validation

### Fixed
- something

## [0.1.0] - 2026-10-20

Impact: release-path

### Added
- the first release

[0.2.0]: https://github.com/owner/store-release/releases/tag/v0.2.0
"""
YANKED = """# Yanked versions

<!-- - vX.Y.Z — YYYY-MM-DD — <reason> (use vX.Y.Z+1) -->
"""


def success(name: str, run_id: int = 1, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion}


class FakeGh:
    def __init__(self, tags: list[str] | None = None, runs: list[dict] | None = None) -> None:
        self.tags = tags or []
        self.runs = [success(name) for name in release_check.REQUIRED_CHECKS] if runs is None else runs
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        path = args[1]
        if "/git/matching-refs/tags/v" in path:
            return json.dumps([{"ref": f"refs/tags/{tag}"} for tag in self.tags])
        if "/check-runs" in path:
            return json.dumps({"total_count": len(self.runs), "check_runs": self.runs})
        raise AssertionError(args)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "store_release").mkdir()
        (self.root / "docs").mkdir()
        self.write_version("0.2.0")
        (self.root / "CHANGELOG.md").write_text(CHANGELOG)
        (self.root / "docs" / "yanked.md").write_text(YANKED)

    def write_version(self, version: str) -> None:
        (self.root / "store_release" / "__init__.py").write_text(f'"""pkg"""\n__version__ = "{version}"\n')

    def check(self, version: str = "0.2.0", *, gh: FakeGh | None = None, env: dict | None = None,
              sha: str = SHA) -> str:
        return release_check.check(version=version, sha=sha, root=self.root, environ=env or ENV, gh=gh or FakeGh())

    def fails(self, pattern: str, **kwargs) -> None:
        with self.assertRaisesRegex(release_check.ReleaseCheckError, pattern):
            self.check(**kwargs)

    def test_every_precondition_holds(self) -> None:
        gh = FakeGh(tags=["v0.1.0", "ios-v7.2", "v0.1.0-rc"])
        notes = self.check(gh=gh)
        self.assertTrue(notes.startswith("Impact: validation\n"))
        self.assertIn("- something", notes)
        self.assertNotIn("0.1.0", notes)
        self.assertEqual([call[1] for call in gh.calls],
                         ["repos/owner/store-release/git/matching-refs/tags/v",
                          f"repos/owner/store-release/commits/{SHA}/check-runs?per_page=100"])

    def test_main_writes_the_notes(self) -> None:
        out = self.root / "notes.md"
        with redirect_stdout(io.StringIO()) as stdout:
            code = release_check.main(["--version", "0.2.0", "--sha", SHA, "--notes-out", str(out)],
                                      gh=FakeGh(), environ=ENV, root=self.root)
        self.assertEqual(code, 0)
        self.assertIn("every precondition holds", stdout.getvalue())
        self.assertEqual(out.read_text(), "Impact: validation\n\n### Fixed\n- something\n")

    def test_main_reports_a_failure(self) -> None:
        out = self.root / "notes.md"
        with redirect_stderr(io.StringIO()) as stderr:
            code = release_check.main(["--version", "0.2.0", "--sha", SHA, "--notes-out", str(out)],
                                      gh=FakeGh(tags=["v0.2.0"]), environ=ENV, root=self.root)
        self.assertEqual(code, 1)
        self.assertIn("error: tag v0.2.0 already exists", stderr.getvalue())
        self.assertFalse(out.exists())

    def test_only_main(self) -> None:
        self.fails("refs/heads/main", env={**ENV, "GITHUB_REF": "refs/heads/feature"})
        self.fails("refs/heads/main", env={"GITHUB_REPOSITORY": ENV["GITHUB_REPOSITORY"]})

    def test_the_version(self) -> None:
        for bad in ("v0.2.0", "0.2", "0.2.0-rc1", "", "0.2.0\n"):
            with self.subTest(version=bad):
                self.fails("is not X.Y.Z", version=bad)
        self.write_version("0.1.9")
        self.fails(r"__version__ is '0\.1\.9', not '0\.2\.0'")
        (self.root / "store_release" / "__init__.py").write_text("VERSION = '0.2.0'\n")
        self.fails("has no __version__")
        (self.root / "store_release" / "__init__.py").write_text("__version__ = '.'.join(['0', '2', '0'])\n")
        self.fails("not a string literal")

    def test_inputs(self) -> None:
        self.fails("not a full commit SHA", sha="0123abc")
        self.fails("is not owner/name", env={**ENV, "GITHUB_REPOSITORY": "owner"})

    def test_tags(self) -> None:
        self.fails("tag v0.2.0 already exists", gh=FakeGh(tags=["v0.1.0", "v0.2.0"]))
        self.fails("not greater than the existing tag v0.10.0", gh=FakeGh(tags=["v0.1.0", "v0.10.0", "v0.3.0"]))
        self.check(gh=FakeGh(tags=["v0.1.9", "v0.1.10"]))

    def test_changelog(self) -> None:
        cases = {
            "## [0.3.0] - 2026-11-02\n\nImpact: validation\n": r"no '## \[0\.2\.0\] - YYYY-MM-DD' section",
            "## [0.2.0] - 2026-11-02\nImpact: validation\n## [0.2.0] - 2026-11-03\nImpact: validation\n": "2 sections",
            "## [0.2.0]\n\nImpact: validation\n": "needs a ' - YYYY-MM-DD' date",
            "## [0.2.0] - 2026-13-40\n\nImpact: validation\n": "needs a ' - YYYY-MM-DD' date",
            "## [0.2.0] - 2026-11-02\n\n\n## [0.1.0] - 2026-10-20\nImpact: validation\n": "section is empty",
            "## [0.2.0] - 2026-11-02\n\n### Fixed\n- x\n": "no 'Impact: ",
            "## [0.2.0] - 2026-11-02\n\nImpact: huge\n": "no 'Impact: ",
            "## [0.2.0] - 2026-11-02\n\n- x\n\n[0.2.0]: https://example.invalid\nImpact: validation\n": "no 'Impact: ",
        }
        for text, pattern in cases.items():
            with self.subTest(changelog=text):
                (self.root / "CHANGELOG.md").write_text(text)
                self.fails(pattern)

    def test_every_impact_is_accepted(self) -> None:
        for impact in release_check.IMPACTS:
            with self.subTest(impact=impact):
                (self.root / "CHANGELOG.md").write_text(f"## [0.2.0] - 2026-11-02\n\nImpact: {impact}\n")
                self.assertEqual(self.check(), f"Impact: {impact}\n")

    def test_yanked(self) -> None:
        (self.root / "docs" / "yanked.md").write_text(YANKED + "- v0.2.0 — 2026-11-03 — broken (use v0.2.1)\n")
        self.fails("docs/yanked.md lists 0.2.0")
        (self.root / "docs" / "yanked.md").write_text(YANKED + "- v0.1.0 — 2026-11-03 — broken (use v0.1.1)\n")
        self.check()

    def test_check_runs(self) -> None:
        names = release_check.REQUIRED_CHECKS
        others = [success(name) for name in names[1:]]
        self.fails(r"test \(stdlib\): no check run", gh=FakeGh(runs=others))
        self.fails(r"test \(stdlib\): still in_progress",
                   gh=FakeGh(runs=others + [success(names[0], status="in_progress", conclusion=None)]))
        self.fails(r"test \(stdlib\): failure", gh=FakeGh(runs=others + [success(names[0], conclusion="failure")]))
        self.fails(r"test \(stdlib\): skipped", gh=FakeGh(runs=others + [success(names[0], conclusion="skipped")]))
        # The newest run of a check decides: a re-run that passed counts, a later failure does not.
        self.check(gh=FakeGh(runs=others + [success(names[0], 5, conclusion="failure"), success(names[0], 9)]))
        self.fails(r"test \(stdlib\): failure",
                   gh=FakeGh(runs=others + [success(names[0], 9, conclusion="failure"), success(names[0], 5)]))
        # Checks that are not required (the canary, the release job itself) are ignored.
        self.check(gh=FakeGh(runs=others + [success(names[0]), success("canary (fud-ai)", 3, conclusion="failure")]))

    def test_the_required_checks_are_this_repositorys_jobs(self) -> None:
        ci = (TOOL_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        codeql = (TOOL_ROOT / ".github" / "workflows" / "codeql.yml").read_text()
        for name in release_check.REQUIRED_CHECKS:
            with self.subTest(check=name):
                if name.startswith("Analyze ("):
                    self.assertIn("name: Analyze (${{ matrix.language }})", codeql)
                    self.assertRegex(codeql, rf"language: \[[^\]]*\b{re.escape(name[9:-1])}\b")
                else:
                    self.assertRegex(ci, rf"(?m)^    name: {re.escape(name)}$")
        self.assertNotIn("canary (fud-ai)", release_check.REQUIRED_CHECKS)

    def test_the_real_files_parse(self) -> None:
        version = release_check.package_version(TOOL_ROOT / "store_release" / "__init__.py")
        release_check.parse_version(version)
        self.assertEqual(release_check.yanked_versions((TOOL_ROOT / "docs" / "yanked.md").read_text()), set())
        changelog = (TOOL_ROOT / "CHANGELOG.md").read_text()
        self.assertRegex(changelog, r"(?m)^## \[Unreleased\]$")


if __name__ == "__main__":
    unittest.main()
