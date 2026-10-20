"""Run store-release's own test suites (hermetic: no network, no app files, temp dirs only).

Profiles:
  --platform ios        tests/unit: test_ios_release, test_core, test_ios_extra
  --platform android    tests/unit: test_android_release, test_core, test_android_extra
  --platform all        every tests/unit/test_*.py
  --suite apps|parity|workflows   the strict CI suites (repeatable; default: unit)
  --pillow-only         only test classes with needs_pillow = True

Skip policy: a skip whose reason starts with "needs Pillow" is allowed only
without --require-pillow (which makes those tests fail instead of skipping);
any other skip fails the run, and with --strict no skip at all is allowed.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
TESTS = TOOL_ROOT / "tests"
SUITES = ("unit", "apps", "parity", "workflows")
PLATFORM_FILES = {
    "ios": ("test_ios_release.py", "test_core.py", "test_ios_extra.py", "test_flows.py"),
    "android": ("test_android_release.py", "test_core.py", "test_android_extra.py", "test_flows.py"),
}
PILLOW_SKIP = "needs Pillow"


class PolicyResult(unittest.TextTestResult):
    """A TextTestResult that also records skips the policy does not allow."""

    require_pillow = False
    strict = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.policy_failures: list[tuple[str, str]] = []

    def addSkip(self, test, reason):  # noqa: N802 - unittest API
        super().addSkip(test, reason)
        allowed = reason.startswith(PILLOW_SKIP) and not self.require_pillow and not self.strict
        if not allowed:
            self.policy_failures.append((test.id(), reason))


def test_files(suite: str, platform: str) -> list[Path]:
    folder = TESTS / suite
    files = sorted(folder.glob("test_*.py"))
    if suite == "unit" and platform in PLATFORM_FILES:
        return [folder / name for name in PLATFORM_FILES[platform] if (folder / name).is_file()]
    if platform == "ios":
        return [path for path in files if "android" not in path.name]
    if platform == "android":
        return [path for path in files if "ios" not in path.name]
    return files


def load_module(path: Path, suite: str):
    name = f"store_release_tests.{suite}.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def build_suite(suites: list[str], platform: str, *, pillow_only: bool) -> unittest.TestSuite:
    if str(TESTS) not in sys.path:
        sys.path.insert(0, str(TESTS))
    if str(TOOL_ROOT) not in sys.path:
        sys.path.insert(0, str(TOOL_ROOT))
    loader = unittest.TestLoader()
    combined = unittest.TestSuite()
    for suite in suites:
        files = test_files(suite, platform)
        if not files:
            raise SystemExit(f"error: no test files for --suite {suite} --platform {platform}")
        for path in files:
            combined.addTests(loader.loadTestsFromModule(load_module(path, suite)))
    if pillow_only:
        combined = unittest.TestSuite(
            test for test in iter_tests(combined) if getattr(type(test), "needs_pillow", False)
        )
    return combined


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="selftest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", choices=("ios", "android", "all"), default="all")
    parser.add_argument("--suite", action="append", choices=SUITES)
    parser.add_argument("--require-pillow", action="store_true")
    parser.add_argument("--pillow-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    suites = args.suite or ["unit"]
    if args.require_pillow:
        os.environ["STORE_RELEASE_TESTS_NEED_PILLOW"] = "1"
    strict = args.strict or os.environ.get("STORE_RELEASE_TESTS_STRICT") == "1"
    if strict:
        os.environ["STORE_RELEASE_TESTS_STRICT"] = "1"

    suite = build_suite(suites, args.platform, pillow_only=args.pillow_only)
    count = suite.countTestCases()
    if count == 0:
        print("error: the selected profile has no tests", file=sys.stderr)
        return 1
    PolicyResult.require_pillow = args.require_pillow
    PolicyResult.strict = strict
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2 if args.verbose else 1,
                                     resultclass=PolicyResult)
    started = time.monotonic()
    result = runner.run(suite)
    elapsed = time.monotonic() - started
    for test_id, reason in result.policy_failures:
        print(f"error: skip not allowed here: {test_id}: {reason}", file=sys.stderr)
    ok = result.wasSuccessful() and not result.policy_failures
    profile = f"--platform {args.platform} --suite {' --suite '.join(suites)}"
    if args.pillow_only:
        profile += " --pillow-only"
    print(f"selftest ({profile}): {count} tests, {len(result.skipped)} skipped, "
          f"{'passed' if ok else 'FAILED'} in {elapsed:.1f}s")
    return 0 if ok else 1
