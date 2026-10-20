"""The docs match the code: every switch, every config key, the docs set and its relative links."""
from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from store_release import switches
from store_release.ios import release as ios_release

TOOL_ROOT = Path(__file__).resolve().parents[2]
DOCS = TOOL_ROOT / "docs"
DOC_SET = ("config.md", "switches.md", "secrets.md", "workflows.md", "rerun.md", "plan.md", "verification.md",
           "release-process.md", "yanked.md", "provenance.md", "roadmap.md")
EXAMPLES = (TOOL_ROOT / "examples" / "fud-ai" / "store-release.toml",
            TOOL_ROOT / "examples" / "example" / "store-release.toml")
# Keys whose value is a table of the app's own names (file paths), documented as one key.
MAP_KEYS = {"ios.creative.sizes"}


def every_switch() -> list[str]:
    names = [name for group in switches.GATES.values() for name in group]
    return names + [ios_release.POLL_SECONDS[0], ios_release.WAIT_MINUTES[0]]


def dotted_keys(data: dict, prefix: str = "") -> set[str]:
    """Every key a config sets, dotted; arrays of tables and empty tables count as one key."""
    keys: set[str] = set()
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value and dotted not in MAP_KEYS:
            keys |= dotted_keys(value, f"{dotted}.")
        else:
            keys.add(dotted)
    return keys


class DocsTests(unittest.TestCase):
    def test_the_docs_set_exists(self) -> None:
        self.assertEqual(sorted(p.name for p in DOCS.glob("*.md")), sorted(DOC_SET))

    def test_gates_env_example_lists_every_switch_once(self) -> None:
        text = (TOOL_ROOT / "gates.env.example").read_text()
        assigned = re.findall(r"(?m)^([A-Z][A-Z0-9_]+)=", text)
        self.assertEqual(sorted(assigned), sorted(every_switch()))
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_switches_md_has_a_row_for_every_switch(self) -> None:
        # The switch tables come before the job-flag table ("What each job sees").
        tables = (DOCS / "switches.md").read_text().split("## What each job sees", 1)[0]
        rows = re.findall(r"(?m)^\| `([A-Z][A-Z0-9_]+)` \|", tables)
        self.assertEqual(sorted(rows), sorted(every_switch()))

    def test_config_md_names_every_key_the_examples_use(self) -> None:
        text = (DOCS / "config.md").read_text()
        documented = set(re.findall(r"(?m)^\| `([a-z0-9_.]+)`", text))
        # "a.b / .c" rows document several keys of one table.
        for row in re.findall(r"(?m)^\| `([a-z0-9_.]+)` / `\.([a-z0-9_]+)`(?: / `\.([a-z0-9_]+)`)?", text):
            base = row[0].rsplit(".", 1)[0]
            documented |= {f"{base}.{part}" for part in row[1:] if part}
        for path in EXAMPLES:
            with self.subTest(config=path.parent.name):
                used = dotted_keys(tomllib.loads(path.read_text()))
                # An empty table ([ios.products]) is documented by its keys.
                missing = sorted(key for key in used if key not in documented
                                 and not any(d.startswith(f"{key}.") for d in documented))
                self.assertEqual(missing, [])

    def test_relative_links_resolve(self) -> None:
        files = [TOOL_ROOT / name for name in ("README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md")]
        files += sorted(DOCS.glob("*.md"))
        for path in files:
            for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
                if re.match(r"[a-z]+:", target) or target.startswith("#"):
                    continue
                with self.subTest(file=path.name, link=target):
                    self.assertTrue((path.parent / target.split("#", 1)[0]).exists(), target)


if __name__ == "__main__":
    unittest.main()
