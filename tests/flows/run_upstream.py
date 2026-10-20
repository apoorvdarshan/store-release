"""Run an app's own store tests (its verbatim copy in tests/parity/upstream_<package>_<sha9>/)
against store-release's port of that app's flow, in a fresh interpreter.

    python3 -I tests/flows/run_upstream.py APP TREE [--list]

TREE (an empty folder) becomes the app's synthetic tree (tests/flows/trees.py); the flow is
configured for it, the flow modules stand in for the app's scripts under their own names
(asc_release, ios_release, ...), and the app's test files run unchanged from TREE/scripts/store/,
so their ROOT is TREE. Tests listed in
tests/flows/<package>.inventory as not ported are left out (each line says why). Prints one
JSON line: {"run": N, "failures": [...], "errors": [...], "skipped": [...]}.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOL_ROOT))

sys.path.insert(0, str(TOOL_ROOT / "tests"))

from flows import trees  # noqa: E402
from store_release import config as config_mod, context, flows  # noqa: E402
from store_release.flows import port  # noqa: E402

TEST_FILES = trees.TEST_FILES


def inventory(app: str) -> dict[str, str]:
    """test id -> reason, for every upstream test that is not run here."""
    package = port.FLOWS[app][0]
    path = TOOL_ROOT / "tests" / "flows" / f"{package}.inventory"
    left_out = {}
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        if not line.strip() or line.startswith("#"):
            continue
        test_id, _, reason = line.partition("  ")
        left_out[test_id.strip()] = reason.strip()
    return left_out


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def short_id(test: unittest.TestCase) -> str:
    # module "upstream_test_ios_release" -> "test_ios_release.<Class>.<method>"
    module, cls, method = test.id().rsplit(".", 2)
    return f"{module.removeprefix('upstream_')}.{cls}.{method}"


def main(argv: list[str]) -> int:
    app, tree = argv[0], Path(argv[1]).resolve()
    list_only = "--list" in argv[2:]
    trees.build_tree(app, tree)
    cfg = config_mod.load_for_app(tree)
    context.configure(cfg, tree)
    package = flows.flow_package(app)
    for name in package.MODULES:
        sys.modules[name] = importlib.import_module(f"{package.__name__}.{name}")
    folder = tree / "scripts" / "store"
    sys.path.insert(0, str(folder))
    suite = unittest.TestSuite()
    for name in TEST_FILES[app]:
        spec = importlib.util.spec_from_file_location(f"upstream_{Path(name).stem}", folder / name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    tests = list(iter_tests(suite))
    if list_only:
        print("\n".join(short_id(t) for t in tests))
        return 0
    left_out = inventory(app)
    unknown = sorted(set(left_out) - {short_id(t) for t in tests})
    kept = unittest.TestSuite(t for t in tests if short_id(t) not in left_out)
    result = unittest.TestResult()
    kept.run(result)
    summary = {
        "app": app, "total": len(tests), "run": result.testsRun, "left_out": len(left_out),
        "unknown_inventory": unknown,
        "failures": [f"{short_id(t)}: {trace.strip().splitlines()[-1]}" for t, trace in result.failures],
        "errors": [f"{short_id(t)}: {trace.strip().splitlines()[-1]}" for t, trace in result.errors],
        "skipped": [f"{short_id(t)}: {reason}" for t, reason in result.skipped],
    }
    print(json.dumps(summary))
    return 0 if not (result.failures or result.errors or unknown) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
