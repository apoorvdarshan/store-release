"""Every one of the 215 store tests ported from fud-ai a99591908 is still in the suite."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

UNIT = Path(__file__).resolve().parent
INVENTORY = UNIT / "PORTED_FROM_FUD_AI.txt"
PORTED = {"test_ios_release": 140, "test_android_release": 54, "test_stage_screenshots": 21}


def listed_ids() -> list[str]:
    return [line.strip() for line in INVENTORY.read_text().splitlines() if line.strip() and not line.startswith("#")]


def ported_module(stem: str):
    loaded = sys.modules.get(f"store_release_tests.unit.{stem}")
    if loaded is not None:
        return loaded
    name = f"store_release_inventory.{stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, UNIT / f"{stem}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def suite_ids() -> set[str]:
    found: set[str] = set()

    def walk(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                found.add(item)

    ids: set[str] = set()
    for stem in PORTED:
        found.clear()
        walk(unittest.defaultTestLoader.loadTestsFromModule(ported_module(stem)))
        ids.update(f"{stem}.{type(test).__name__}.{test._testMethodName}" for test in found)
    return ids


class PortInventoryTests(unittest.TestCase):
    def test_the_inventory_lists_215_tests(self) -> None:
        ids = listed_ids()
        self.assertEqual(len(ids), 215)
        self.assertEqual(len(set(ids)), 215)
        for stem, count in PORTED.items():
            with self.subTest(stem=stem):
                self.assertEqual(sum(1 for i in ids if i.startswith(stem + ".")), count)

    def test_every_listed_test_is_in_the_suite(self) -> None:
        present = suite_ids()
        missing = [test_id for test_id in listed_ids() if test_id not in present]
        self.assertEqual(missing, [], "ported fud-ai tests missing from tests/unit")


if __name__ == "__main__":
    unittest.main()
