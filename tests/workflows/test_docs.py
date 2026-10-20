"""The docs match the code, so they cannot drift: every config key the parser reads, every switch, every
secret each reusable workflow declares, every message the troubleshooting page quotes, the docs set and
its relative links."""
from __future__ import annotations

import ast
import contextlib
import re
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import yaml

from store_release import config, switches
from store_release.ios import release as ios_release

TOOL_ROOT = Path(__file__).resolve().parents[2]
DOCS = TOOL_ROOT / "docs"
DOC_SET = ("config.md", "switches.md", "secrets.md", "workflows.md", "rerun.md", "plan.md", "verification.md",
           "release-process.md", "yanked.md", "provenance.md", "roadmap.md", "flows.md", "troubleshooting.md")
# Every config the repository ships: the apps' examples, the non-app fixture and the templates.
CONFIGS = tuple(sorted((TOOL_ROOT / "examples").glob("*/store-release.toml"))
                + sorted((TOOL_ROOT / "examples" / "templates").glob("*/store-release.toml")))
# The _Table methods that read a key (has() only probes; the key is read by one of these next).
READERS = ("raw", "str", "bool", "int", "enum", "list", "path", "table")
WORKFLOWS = TOOL_ROOT / ".github" / "workflows"
REUSABLE = ("ios-prepare.yml", "ios-build.yml", "ios-wait-for-build.yml", "ios-app-store-connect.yml",
            "ios-source-archive.yml", "ios-github-release.yml", "ios-store-plan.yml", "android-gates.yml",
            "android-publish.yml", "android-store-plan.yml", "checks.yml")
ROW = re.compile(r"^\| `([a-z0-9_.\[\]]+)` \|(.*)\|\s*$")


def every_switch() -> list[str]:
    names = [name for group in switches.GATES.values() for name in group]
    return names + [ios_release.POLL_SECONDS[0], ios_release.WAIT_MINUTES[0]]


def parser_keys() -> tuple[set[str], set[str]]:
    """(leaf keys, table keys) the parser reads across every shipped config, dotted with [] for array items.

    Recorded from _Table itself, so a key the parser starts reading appears here without any list to update.
    """
    leaves: set[str] = set()
    tables: set[str] = set()
    original_raw, original_table = config._Table.raw, config._Table.table

    def name_of(table, name: str) -> str:
        return re.sub(r"\[\d+\]", "[]", table.key(name))

    def raw(self, name, *args, **kwargs):
        leaves.add(name_of(self, name))
        return original_raw(self, name, *args, **kwargs)

    def table(self, name):
        tables.add(name_of(self, name))
        return original_table(self, name)

    with mock.patch.object(config._Table, "raw", raw), mock.patch.object(config._Table, "table", table):
        for path in CONFIGS:
            config.parse(tomllib.loads(path.read_text(encoding="utf-8")))
    # table() reads through raw(); a key read as a table is a table, not a leaf.
    return leaves - tables, tables


def literal_key_names() -> set[str]:
    """Every key name config.py passes to a _Table reader or to a helper that reads one (static, from the AST)."""
    tree = ast.parse((TOOL_ROOT / "store_release" / "config.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in READERS and node.args:
            argument = node.args[0]
        elif isinstance(func, ast.Name) and func.id in ("_hooks", "_patterns", "_bundle_ids") and len(node.args) > 1:
            argument = node.args[1]
        else:
            continue
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            names.add(argument.value)
    return names


def config_rows() -> dict[str, list[str]]:
    """{documented key: its cells (type, default, example, meaning)} from docs/config.md's key tables."""
    rows: dict[str, list[str]] = {}
    for line in (DOCS / "config.md").read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in match.group(2).split(" | ")]
    return rows


def code_strings() -> str:
    """Every string the package and the reusable workflows can print, with <> where a value is filled in."""
    parts: list[str] = []
    for path in sorted((TOOL_ROOT / "store_release").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.JoinedStr):
                parts.append("".join(value.value if isinstance(value, ast.Constant) else "<>"
                                     for value in node.values))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts.append(node.value)
    parts += [path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))]
    return "\n".join(parts)


class DocsTests(unittest.TestCase):
    def test_the_docs_set_exists(self) -> None:
        self.assertEqual(sorted(p.name for p in DOCS.glob("*.md")), sorted(DOC_SET))

    def test_gates_env_example_lists_every_switch_once(self) -> None:
        text = (TOOL_ROOT / "gates.env.example").read_text()
        assigned = re.findall(r"(?m)^([A-Z][A-Z0-9_]+)=", text)
        self.assertEqual(sorted(assigned), sorted(every_switch()))
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_switches_md_has_a_row_with_its_default_for_every_switch(self) -> None:
        # The switch tables come before the job-flag table ("What each job sees").
        tables = (DOCS / "switches.md").read_text().split("## What each job sees", 1)[0]
        rows = dict(re.findall(r"(?m)^\| `([A-Z][A-Z0-9_]+)` \| ([^|]+) \|", tables))
        self.assertEqual(sorted(rows), sorted(every_switch()))
        numbers = {ios_release.POLL_SECONDS[0]: str(ios_release.POLL_SECONDS[1]),
                   ios_release.WAIT_MINUTES[0]: str(ios_release.WAIT_MINUTES[1])}
        for name, default in rows.items():
            with self.subTest(switch=name):
                self.assertEqual(default.strip(), numbers.get(name, "off"))

    def test_config_md_documents_every_key_the_parser_reads(self) -> None:
        leaves, tables = parser_keys()
        rows = config_rows()
        self.assertEqual(sorted(leaves - set(rows)), [], "keys the parser reads but config.md does not list")
        self.assertEqual(sorted(set(rows) - leaves), [], "keys config.md lists but the parser does not read")
        text = (DOCS / "config.md").read_text(encoding="utf-8")
        for table in sorted(tables):
            with self.subTest(table=table):
                self.assertTrue(f"[{table}]" in text or f"[[{table}]]" in text or f"[[{table}.sets]]" in text,
                                f"config.md never names the table [{table}]")

    def test_the_shipped_configs_reach_every_key_config_py_reads(self) -> None:
        # The recorded keys cover the parser only if the shipped configs reach every branch: each key name
        # in config.py must be the last part of a recorded key. A new key needs a config that uses it.
        leaves, tables = parser_keys()
        recorded = {key.rsplit(".", 1)[-1].removesuffix("[]") for key in leaves | tables}
        self.assertEqual(sorted(literal_key_names() - recorded), [])

    def test_every_config_row_has_a_type_a_default_and_a_toml_example(self) -> None:
        for key, cells in config_rows().items():
            with self.subTest(key=key):
                self.assertEqual(len(cells), 4, f"{key}: expected Type | Default | Example | Meaning")
                kind, default, example, meaning = cells
                self.assertTrue(kind and default and meaning, key)
                values = re.findall(r"`([^`]+)`", example)
                self.assertTrue(values, f"{key}: the example is not a `value`")
                tomllib.loads(f"x = {values[0]}")

    def test_secrets_md_matches_every_reusable_workflows_secrets(self) -> None:
        text = (DOCS / "secrets.md").read_text(encoding="utf-8")
        section = text.split("## Per workflow", 1)[1].split("\n## ", 1)[0]
        documented: dict[str, dict[str, bool]] = {}
        for workflow, secret, required in re.findall(r"(?m)^\| `([a-z-]+\.yml)` \|([^|]+)\|([^|]*)\|", section):
            names = re.findall(r"`([A-Z0-9_]+)`", secret)
            entry = documented.setdefault(workflow, {})
            for name in names:
                self.assertNotIn(name, entry, f"{workflow} lists {name} twice")
                entry[name] = required.strip() == "yes"
        for name in REUSABLE:
            data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
            declared = (data[True]["workflow_call"].get("secrets") or {})
            expected = {secret: bool((spec or {}).get("required")) for secret, spec in declared.items()}
            with self.subTest(workflow=name):
                self.assertIn(name, documented, f"secrets.md has no row for {name}")
                self.assertEqual(documented[name], expected)
        self.assertEqual(sorted(documented), sorted(REUSABLE))

    def test_every_message_troubleshooting_quotes_is_in_the_code(self) -> None:
        corpus = code_strings()
        headings = re.findall(r"(?m)^### `([^`]+)`", (DOCS / "troubleshooting.md").read_text(encoding="utf-8"))
        self.assertGreater(len(headings), 15)
        for heading in headings:
            pieces = [piece for piece in re.split(r"<[^>]+>", heading) if piece.strip()]
            with self.subTest(message=heading):
                self.assertTrue(pieces)
                for piece in pieces:
                    self.assertIn(piece, corpus, f"{piece!r} is not a message the code prints")

    def test_relative_links_resolve(self) -> None:
        files = [TOOL_ROOT / name for name in ("README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md")]
        files += sorted(DOCS.glob("*.md")) + sorted((TOOL_ROOT / "examples").rglob("*.md"))
        for path in files:
            for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
                if re.match(r"[a-z]+:", target) or target.startswith("#"):
                    continue
                with self.subTest(file=path.name, link=target):
                    self.assertTrue((path.parent / target.split("#", 1)[0]).exists(), target)

    def test_no_user_facing_page_promises_fud_ai_parity(self) -> None:
        # The history of the code (Fud AI's pipeline, its parity) lives in provenance.md.
        for path in [TOOL_ROOT / "README.md", *(DOCS / name for name in DOC_SET if name != "provenance.md")]:
            with self.subTest(page=path.name), contextlib.suppress(FileNotFoundError):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?i)fud ai('s)? (parity|behaviour|pipeline)")


if __name__ == "__main__":
    unittest.main()
