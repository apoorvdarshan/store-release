"""The PyPI package: pyproject.toml, the description hook (hatch_build.py), scripts/dists.py and the installed
command's entry points (cli.console_main, python -m store_release).

Standard library only: hatchling is stubbed and PyPI is faked, and nothing is built here. CI's package job builds
the sdist and the wheel and runs the wheel alone on every supported Python (scripts/dists.py check)."""
from __future__ import annotations

import ast
import contextlib
import email.message
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release import __version__, cli, scaffold, selftest, smoke
from support import apps as support_apps

TOOL_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((TOOL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
REPO_URL = "https://github.com/apoorvdarshan/store-release"
PIN = re.compile(r"^([A-Za-z0-9_.-]+)(\[[a-z0-9,]+\])?==([0-9][0-9A-Za-z.]*)\b", re.M)
RANGE = re.compile(r"^([A-Za-z0-9_.-]+)(\[[a-z0-9,]+\])?>=([0-9.]+),<([0-9]+)$")


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"store_release_scripts_{name}", TOOL_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_hatch_build():
    """hatch_build.py with a stand-in for hatchling's MetadataHookInterface (hatchling is not installed here)."""

    class MetadataHookInterface:
        def __init__(self, root: str, config: dict) -> None:
            self.root = root
            self.config = config

    interface = types.ModuleType("hatchling.metadata.plugin.interface")
    interface.MetadataHookInterface = MetadataHookInterface
    stubs = {name: types.ModuleType(name) for name in ("hatchling", "hatchling.metadata", "hatchling.metadata.plugin")}
    stubs["hatchling.metadata.plugin.interface"] = interface
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("store_release_hatch_build", TOOL_ROOT / "hatch_build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


dists = load_script("dists")
hatch_build = load_hatch_build()


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def top_level_pins(*files: str) -> dict[str, tuple[str, str]]:
    """{normalized name: (extras, version)} of the == pins in requirements .in files."""
    pins = {}
    for name in files:
        for match in PIN.finditer((TOOL_ROOT / name).read_text(encoding="utf-8")):
            pins[normalized(match[1])] = (match[2] or "", match[3])
    return pins


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def extra_problems(extras: dict[str, list[str]], locked: dict[str, tuple[str, str]]) -> list[str]:
    """The extras rule: each locked runtime dependency is in exactly one extra, with the lock's own extras, as
    >=<floor>,<<the floor's next major>, a range that holds the locked version (the one CI tests). Dependabot then
    moves the lock within the major and leaves the range alone; a new major needs its floor and cap moved."""
    problems: list[str] = []
    found: dict[str, str] = {}
    for extra, requirements in sorted(extras.items()):
        if extra == "all":
            continue
        for requirement in requirements:
            match = RANGE.fullmatch(requirement)
            if match is None:
                problems.append(f"[{extra}] {requirement}: write >=<floor>,<<the floor's next major>")
                continue
            name = normalized(match[1])
            if name not in locked:
                problems.append(f"[{extra}] {requirement}: not in requirements.in or requirements-checks.in")
                continue
            if name in found:
                problems.append(f"[{extra}] {requirement}: {name} is in [{found[name]}] too")
            found[name] = extra
            lock_extras, pinned = locked[name]
            if (match[2] or "") != lock_extras:
                problems.append(f"[{extra}] {requirement}: the lock installs {name}{lock_extras}")
            floor, cap = version_tuple(match[3]), int(match[4])
            if cap != floor[0] + 1:
                problems.append(f"[{extra}] {requirement}: the cap is not the floor's next major")
            if not floor <= version_tuple(pinned) < (cap,):
                problems.append(f"[{extra}] {requirement}: the lock pins {pinned}, outside the range (a new "
                                "major: move the floor to it and the cap past it)")
    problems.extend(f"{name}: locked, but in no extra" for name in sorted(set(locked) - set(found)))
    return problems


class PyprojectTests(unittest.TestCase):
    def test_the_project(self) -> None:
        self.assertEqual(PROJECT["name"], "store-release")
        self.assertEqual(sorted(PROJECT["dynamic"]), ["readme", "version"])
        self.assertEqual(PYPROJECT["tool"]["hatch"]["version"], {"path": "store_release/__init__.py"})
        self.assertEqual(PROJECT["license"], "MIT")
        self.assertEqual(PROJECT["license-files"], ["LICENSE"])
        self.assertTrue((TOOL_ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"))
        # The standard library covers init, callers render and the checks; the extras add the rest.
        self.assertEqual(PROJECT["dependencies"], [])
        self.assertEqual(PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"], {"packages": ["store_release"]})

    def test_the_console_script_is_the_cli(self) -> None:
        self.assertEqual(PROJECT["scripts"], {"store-release": "store_release.cli:console_main"})
        self.assertTrue(callable(cli.console_main))
        self.assertEqual(dists.CONSOLE_SCRIPT, "store-release = store_release.cli:console_main")

    def test_the_supported_pythons_are_what_ci_runs_the_wheel_on(self) -> None:
        floor = re.fullmatch(r">=3\.(\d+)", PROJECT["requires-python"])
        self.assertIsNotNone(floor, PROJECT["requires-python"])
        minors = sorted(int(m) for c in PROJECT["classifiers"]
                        if (m := (re.fullmatch(r"Programming Language :: Python :: 3\.(\d+)", c) or [None, None])[1]))
        self.assertEqual(minors[0], int(floor[1]))
        self.assertEqual(minors, list(range(minors[0], minors[-1] + 1)), "a supported minor is missing")
        ci = (TOOL_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        package = ci.split("\n  package:\n", 1)[1].split("\n  workflows:\n", 1)[0]
        smoked = sorted(int(m) for m in re.findall(r"--python python3\.(\d+)", package))
        installed = sorted(int(m) for m in re.findall(r"(?m)^ {12}3\.(\d+)$", package))
        self.assertEqual(smoked, minors)
        self.assertEqual(installed, minors)
        # tomllib is the floor's reason: store-release reads TOML with the standard library.
        self.assertGreaterEqual(int(floor[1]), 11)
        ruff = (TOOL_ROOT / "ruff.toml").read_text(encoding="utf-8")
        self.assertIn(f'target-version = "py3{floor[1]}"', ruff)

    def test_each_extra_holds_the_locked_version(self) -> None:
        extras = PROJECT["optional-dependencies"]
        self.assertEqual(sorted(extras), ["all", "android", "checks", "images", "ios"])
        self.assertEqual(extra_problems(extras, top_level_pins("requirements.in", "requirements-checks.in")), [])
        everything = re.fullmatch(r"store-release\[([a-z,]+)\]", extras["all"][0])
        self.assertEqual(len(extras["all"]), 1)
        self.assertEqual(sorted(everything[1].split(",")), sorted(set(extras) - {"all"}))

    def test_the_extras_rule_takes_dependabots_lock_bumps(self) -> None:
        """Dependabot treats pyproject.toml as a library's (it has a name and a description), so it widens a range
        only when the new version falls outside it: a lock bump within the major leaves the extra as it is."""
        extras = {"ios": ["PyJWT[crypto]>=2.15.1,<3"], "all": ["store-release[ios]"]}
        self.assertEqual(extra_problems(extras, {"pyjwt": ("[crypto]", "2.15.1")}), [])
        self.assertEqual(extra_problems(extras, {"pyjwt": ("[crypto]", "2.16.0")}), [])
        # A new major: Dependabot moves the cap to <4 and keeps the floor, which must move too.
        self.assertEqual(extra_problems({"ios": ["PyJWT[crypto]>=2.15.1,<4"]}, {"pyjwt": ("[crypto]", "3.0.0")}),
                         ["[ios] PyJWT[crypto]>=2.15.1,<4: the cap is not the floor's next major"])
        self.assertEqual(extra_problems(extras, {"pyjwt": ("[crypto]", "3.0.0")}),
                         ["[ios] PyJWT[crypto]>=2.15.1,<3: the lock pins 3.0.0, outside the range (a new major: "
                          "move the floor to it and the cap past it)"])
        self.assertEqual(extra_problems({"ios": ["PyJWT[crypto]>=3.0.0,<4"]}, {"pyjwt": ("[crypto]", "3.0.0")}), [])
        cases = {
            "floor above the lock": ({"ios": ["PyJWT[crypto]>=2.16.0,<3"]}, {"pyjwt": ("[crypto]", "2.15.1")},
                                     "the lock pins 2.15.1, outside the range"),
            "not a range": ({"ios": ["PyJWT[crypto]==2.15.1"]}, {"pyjwt": ("[crypto]", "2.15.1")}, "write >="),
            "the lock's extras": ({"ios": ["PyJWT>=2.15.1,<3"]}, {"pyjwt": ("[crypto]", "2.15.1")},
                                  "the lock installs pyjwt[crypto]"),
            "not locked": ({"images": ["Pillow>=13.0.0,<14"]}, {}, "not in requirements.in"),
            "in two extras": ({"a": ["PyYAML>=6.0.3,<7"], "b": ["PyYAML>=6.0.3,<7"]}, {"pyyaml": ("", "6.0.3")},
                              "pyyaml is in [a] too"),
            "in no extra": ({}, {"pyyaml": ("", "6.0.3")}, "pyyaml: locked, but in no extra"),
        }
        for label, (case, locked, problem) in cases.items():
            with self.subTest(case=label):
                problems = extra_problems(case, locked)
                self.assertTrue(any(problem in found for found in problems), problems)

    def test_the_build_tools_are_hash_locked(self) -> None:
        self.assertEqual(PYPROJECT["build-system"], {"requires": ["hatchling>=1.27.0"],
                                                     "build-backend": "hatchling.build"})
        lock = (TOOL_ROOT / "requirements-dist.txt").read_text(encoding="utf-8")
        pins = {normalized(m[1]): m[3] for m in PIN.finditer(lock)}
        self.assertGreaterEqual(version_tuple(pins["hatchling"]), (1, 27, 0))
        wanted = top_level_pins("requirements-dist.in")
        self.assertTrue({"build", "hatchling", "twine"} <= set(wanted))
        for name, (_, version) in wanted.items():
            self.assertEqual(pins.get(name), version, f"requirements-dist.txt does not lock {name}=={version}")
        # Every pin carries hashes (pip install --require-hashes).
        blocks = re.split(r"(?m)^(?=[a-z0-9])", lock.split("\n\n", 1)[-1])
        for block in (b for b in blocks if "==" in b):
            with self.subTest(pin=block.split()[0]):
                self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")

    def test_the_sdist_holds_what_dists_check_expects(self) -> None:
        include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        self.assertEqual(include[0], "/store_release")
        self.assertEqual(sorted(name.lstrip("/") for name in include[1:]),
                         sorted(set(dists.SDIST_FILES) - {"PKG-INFO", "pyproject.toml", ".gitignore"}))
        for name in include:
            self.assertTrue((TOOL_ROOT / name.lstrip("/")).exists(), name)

    def test_the_project_urls(self) -> None:
        urls = PROJECT["urls"]
        self.assertEqual(sorted(urls), ["Changelog", "Documentation", "Homepage", "Issues", "Source"])
        for label, url in urls.items():
            with self.subTest(url=label):
                self.assertTrue(url == REPO_URL or url.startswith(REPO_URL + "/"), url)
                path = re.sub(rf"^{re.escape(REPO_URL)}/(?:blob|tree)/main/", "", url)
                if path != url:
                    self.assertTrue((TOOL_ROOT / path).exists(), url)


class DescriptionHookTests(unittest.TestCase):
    """hatch_build.py: the PyPI description is the README with its relative links pointed at the tag."""

    def test_relative_links_point_at_the_tag(self) -> None:
        base = f"{hatch_build.REPOSITORY}"
        text = ("See [config](docs/config.md), [templates](examples/templates/), [a part](docs/plan.md#steps),\n"
                "[here](./LICENSE) and ![logo](docs/logo.png); [site](https://example.com), [up](#top),\n"
                "[mail](mailto:someone@example.com) and [root](/abs).\n"
                "```bash\n"
                "echo [not a link](docs/config.md)\n"
                "```\n"
                "~~~\n"
                "[also not](docs/x.md)\n"
                "~~~\n"
                "[after](CHANGELOG.md)\n")
        self.assertEqual(hatch_build.absolute_links(text, "1.2.3"), (
            f"See [config]({base}/blob/v1.2.3/docs/config.md), [templates]({base}/tree/v1.2.3/examples/templates/), "
            f"[a part]({base}/blob/v1.2.3/docs/plan.md#steps),\n"
            f"[here]({base}/blob/v1.2.3/LICENSE) and ![logo]({base}/raw/v1.2.3/docs/logo.png); "
            "[site](https://example.com), [up](#top),\n"
            "[mail](mailto:someone@example.com) and [root](/abs).\n"
            "```bash\n"
            "echo [not a link](docs/config.md)\n"
            "```\n"
            "~~~\n"
            "[also not](docs/x.md)\n"
            "~~~\n"
            f"[after]({base}/blob/v1.2.3/CHANGELOG.md)\n"))

    def test_the_readme_keeps_no_relative_link_and_each_resolves(self) -> None:
        readme = (TOOL_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue(dists.relative_links(readme), "the README has relative links to rewrite")
        text = hatch_build.absolute_links(readme, __version__)
        self.assertEqual(dists.relative_links(text), [])
        prefix = re.compile(rf"^{re.escape(hatch_build.REPOSITORY)}/(?:blob|tree|raw)/v{re.escape(__version__)}/")
        rewritten = [target for target in re.findall(r"\]\(([^)\s]+)\)", text) if prefix.match(target)]
        self.assertEqual(len(rewritten), len(dists.relative_links(readme)))
        for target in rewritten:
            with self.subTest(link=target):
                self.assertTrue((TOOL_ROOT / prefix.sub("", target).split("#", 1)[0]).exists(), target)

    def test_the_hook_sets_the_description(self) -> None:
        hook = hatch_build.PypiReadmeHook(str(TOOL_ROOT), {})
        metadata = {"name": "store-release", "version": "9.8.7"}
        hook.update(metadata)
        self.assertEqual(metadata["readme"]["content-type"], "text/markdown")
        self.assertIn(f"({hatch_build.REPOSITORY}/blob/v9.8.7/docs/config.md)", metadata["readme"]["text"])
        self.assertTrue(metadata["readme"]["text"].startswith("# store-release\n"))


class FakePypi:
    """urlopen for PyPI's JSON API and integrity API: {file name: sha256} and {file name: [publisher]}."""

    def __init__(self, files: dict[str, str] | None = None, publishers: dict[str, list[dict]] | None = None,
                 errors: list[int] | None = None) -> None:
        self.files = files
        self.publishers = publishers or {}
        self.errors = list(errors or [])
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        if self.errors:
            raise urllib.error.HTTPError(url, self.errors.pop(0), "error", {}, None)
        if "/integrity/" in url:
            name = url.split("/")[-2]
            if name not in self.publishers:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            body = {"version": 1, "attestation_bundles": [
                {"publisher": publisher, "attestations": [{"version": 1}]} for publisher in self.publishers[name]]}
        else:
            if self.files is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            body = {"info": {"version": "1.2.3"}, "urls": [
                {"filename": name, "digests": {"sha256": digest}} for name, digest in self.files.items()]}
        return contextlib.nullcontext(io.BytesIO(json.dumps(body).encode()))


PUBLISHER = {"kind": "GitHub", "repository": "owner/store-release", "workflow": "release.yml", "environment": "pypi"}
SHA = "a" * 40


class FakeGh:
    """gh for `release view --json isDraft,isImmutable,assets` and the tag's ref; the first failures calls fail."""

    def __init__(self, release: dict | None, tagged: str = SHA, failures: int = 0) -> None:
        self.release, self.tagged, self.failures = release, tagged, failures
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if self.failures:
            self.failures -= 1
            raise dists.GhError("gh release view failed: HTTP 502")
        if args[:2] == ["release", "view"]:
            if self.release is None:
                raise dists.GhError("gh release view failed: release not found")
            return json.dumps(self.release)
        if args[0] == "api" and args[1].endswith("/git/ref/tags/v1.2.3"):
            return self.tagged + "\n"
        raise AssertionError(args)


class ChangingGh(FakeGh):
    """FakeGh whose `release view` answers each of states in turn, then the last one."""

    def __init__(self, states: list[dict]) -> None:
        super().__init__(states[0])
        self.states = list(states)

    def __call__(self, args: list[str]) -> str:
        if args[:2] == ["release", "view"]:
            self.release = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return super().__call__(args)


class DistsTests(unittest.TestCase):
    """scripts/dists.py without building: names, the PyPI plan and the PyPI check."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dist = self.tmp / "dist"
        self.dist.mkdir()
        self.sdist, self.wheel = dists.dist_names("1.2.3")
        (self.dist / self.sdist).write_bytes(b"sdist bytes")
        (self.dist / self.wheel).write_bytes(b"wheel bytes")
        self.digests = {self.sdist: hashlib.sha256(b"sdist bytes").hexdigest(),
                        self.wheel: hashlib.sha256(b"wheel bytes").hexdigest()}

    def test_names_and_the_version(self) -> None:
        self.assertEqual(dists.dist_names("1.2.3"), ("store_release-1.2.3.tar.gz", "store_release-1.2.3-py3-none-any.whl"))
        for bad in ("1.2", "v1.2.3", "1.2.3rc1", ""):
            with self.subTest(version=bad), self.assertRaises(dists.DistError):
                dists.dist_names(bad)
        self.assertEqual(dists.package_version(TOOL_ROOT), __version__)

    def test_the_folder_holds_exactly_the_two_files(self) -> None:
        self.assertEqual(sorted(dists.the_dists(self.dist, "1.2.3")), sorted([self.sdist, self.wheel]))
        with self.assertRaisesRegex(dists.DistError, "not exactly"):
            dists.the_dists(self.dist, "1.2.4")
        (self.dist / "store_release-1.2.3.tar.gz.sha256").write_text("x")
        with self.assertRaisesRegex(dists.DistError, "not exactly"):
            dists.the_dists(self.dist, "1.2.3")

    def test_the_templates_are_what_init_writes(self) -> None:
        for name, args in scaffold.TEMPLATES.items():
            with self.subTest(template=name):
                ns = scaffold.parser().parse_args([*args, "--sha", scaffold.PLACEHOLDER_SHA, "--allow-untagged"])
                ns.config_path = scaffold.config_mod.DEFAULT_CONFIG
                answers = scaffold.collect(ns, Path("/nonexistent-template-root"), ask=None, environ={})
                self.assertEqual(dists.template_files(name), scaffold.planned_files(answers, sha=scaffold.PLACEHOLDER_SHA))

    def metadata(self, **changes: str) -> str:
        """Core metadata as hatchling writes it for pyproject.toml, with changes (_body: the description)."""
        body = changes.pop("_body", f"# store-release\n\n[config]({REPO_URL}/blob/v1.2.3/docs/config.md)\n")
        message = email.message.Message()
        fields = {"Metadata-Version": "2.5", "Name": "store-release", "Version": "1.2.3",
                  "Requires-Python": PROJECT["requires-python"], "License-Expression": "MIT",
                  "Description-Content-Type": "text/markdown", **changes}
        for field, value in fields.items():
            message[field] = value
        for extra in PROJECT["optional-dependencies"]:
            message["Provides-Extra"] = extra
        message["Requires-Dist"] = "pyyaml<7,>=6.0.3; extra == 'checks'"
        message.set_payload(body)
        return message.as_string()

    def test_the_metadata_checks(self) -> None:
        dists.check_metadata(self.metadata(), "1.2.3", "wheel", TOOL_ROOT)
        cases = {"Version": ("1.2.4", "Version is '1.2.4'"), "License-Expression": ("Apache-2.0", "License-Expression"),
                 "Requires-Python": (">=3.9", "Requires-Python"),
                 "_body": ("# store-release\n\n[config](docs/config.md)\n", "relative links")}
        for field, (value, error) in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(dists.DistError, re.escape(error)):
                dists.check_metadata(self.metadata(**{field: value}), "1.2.3", "wheel", TOOL_ROOT)
        text = self.metadata().replace("Requires-Dist: pyyaml<7,>=6.0.3; extra == 'checks'", "Requires-Dist: pyyaml")
        with self.assertRaisesRegex(dists.DistError, "outside the extras"):
            dists.check_metadata(text, "1.2.3", "wheel", TOOL_ROOT)

    def plan(self, fake: FakePypi) -> tuple[list[str], Path, str]:
        upload, output = self.tmp / "upload", self.tmp / "github_output"
        missing = dists.pypi_plan(self.dist, "1.2.3", upload, opener=fake, environ={"GITHUB_OUTPUT": str(output)})
        return missing, upload, output.read_text()

    def test_the_plan_uploads_what_pypi_lacks(self) -> None:
        with redirect_stdout(io.StringIO()):
            missing, upload, output = self.plan(FakePypi(files=None))
        self.assertEqual(missing, [self.sdist, self.wheel])
        self.assertEqual(sorted(p.name for p in upload.iterdir()), sorted([self.sdist, self.wheel]))
        self.assertEqual(output, "upload=true\n")

    def test_a_rerun_skips_the_files_pypi_has(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            missing, upload, output = self.plan(FakePypi(files={self.sdist: self.digests[self.sdist]}))
        self.assertEqual(missing, [self.wheel])
        self.assertEqual([p.name for p in upload.iterdir()], [self.wheel])
        self.assertIn(f"already on PyPI with this SHA-256: {self.sdist}", out.getvalue())
        self.assertEqual(output, "upload=true\n")
        self.tmp.joinpath("github_output").unlink()
        for path in upload.iterdir():
            path.unlink()
        with redirect_stdout(io.StringIO()):
            missing, upload, output = self.plan(FakePypi(files=dict(self.digests)))
        self.assertEqual((missing, list(upload.iterdir()), output), ([], [], "upload=false\n"))

    def test_the_plan_refuses_another_file_or_digest(self) -> None:
        cases = {"another digest": {self.sdist: "0" * 64},
                 "a file the release did not build": {**self.digests, "store_release-1.2.3-py3-none-manylinux.whl": "1" * 64}}
        for label, files in cases.items():
            with self.subTest(case=label), self.assertRaises(dists.PypiMismatch):
                self.plan(FakePypi(files=files))
            self.assertFalse((self.tmp / "upload").exists(), "nothing is copied when the plan fails")

    def test_the_plan_fails_closed_when_pypi_cannot_be_read(self) -> None:
        with self.assertRaisesRegex(dists.DistError, "HTTP 503") as caught:
            self.plan(FakePypi(files=None, errors=[503]))
        self.assertNotIsInstance(caught.exception, dists.PypiMismatch)
        (self.tmp / "upload").mkdir()
        (self.tmp / "upload" / "left-over").write_text("x")
        with self.assertRaisesRegex(dists.DistError, "new or empty"):
            self.plan(FakePypi(files=None))

    def verify(self, fake: FakePypi, *, timeout: float = 60) -> tuple[list[float], str]:
        slept: list[float] = []
        now = [0.0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        out = io.StringIO()
        with redirect_stdout(out):
            dists.pypi_verify(self.dist, "1.2.3", repository="owner/store-release", opener=fake, timeout=timeout,
                              interval=15, sleep=sleep, clock=lambda: now[0])
        return slept, out.getvalue()

    def test_verify_waits_for_the_files_and_their_attestations(self) -> None:
        fake = FakePypi(files=None)
        original = fake.__call__

        def later(request, timeout=None):
            if len(fake.urls) >= 2:
                fake.files = dict(self.digests)
                fake.publishers = {name: [PUBLISHER] for name in self.digests}
            return original(request, timeout)

        slept, out = self.verify(later)
        self.assertEqual(slept, [15, 15])
        self.assertIn(f"PyPI serves {self.wheel}  sha256 {self.digests[self.wheel]}, attested by owner/store-release "
                      "release.yml", out)
        self.assertNotIn("::warning::", out)

    def test_verify_accepts_case_and_extra_keys_and_warns_without_the_environment(self) -> None:
        publisher = {"kind": "GitHub", "repository": "Owner/Store-Release", "workflow": "release.yml",
                     "environment": None, "claims": {}}
        _, out = self.verify(FakePypi(files=dict(self.digests), publishers={n: [publisher] for n in self.digests}))
        self.assertIn("::warning::PyPI's trusted publisher for store-release does not require the 'pypi' environment", out)

    def test_verify_fails_on_another_digest_at_once_and_on_a_timeout(self) -> None:
        with self.assertRaises(dists.PypiMismatch):
            self.verify(FakePypi(files={self.sdist: "0" * 64, self.wheel: self.digests[self.wheel]}))
        other = {**PUBLISHER, "repository": "someone/else"}
        with self.assertRaisesRegex(dists.DistError, r"after 60s PyPI still lacks .*attestation by owner/store-release"):
            self.verify(FakePypi(files=dict(self.digests), publishers={n: [other] for n in self.digests}))
        with self.assertRaisesRegex(dists.DistError, "a readable answer"):
            self.verify(FakePypi(files=None, errors=[500] * 10))
        slept, _ = self.verify(FakePypi(files=dict(self.digests), publishers={n: [PUBLISHER] for n in self.digests},
                                        errors=[502]))
        self.assertEqual(slept, [15])

    def release(self, **changes) -> dict:
        assets = [{"name": name, "digest": f"sha256:{digest}"} for name, digest in self.digests.items()]
        return {"isDraft": False, "isImmutable": True, "assets": assets, **changes}

    def release_check(self, gh: FakeGh, *, complete: bool = True) -> tuple[list[float], str]:
        slept: list[float] = []
        out = io.StringIO()
        with redirect_stdout(out):
            dists.release_check(self.dist, "1.2.3", sha=SHA, repository="owner/store-release", complete=complete,
                                gh=gh, sleep=slept.append)
        return slept, out.getvalue()

    def test_the_release_must_be_exactly_this_runs(self) -> None:
        gh = FakeGh(self.release())
        _, out = self.release_check(gh)
        self.assertIn(f"v1.2.3 is published and immutable on {SHA}, with exactly these 2 files", out)
        self.assertEqual(gh.calls, [["release", "view", "v1.2.3", "--repo", "owner/store-release", "--json",
                                     "isDraft,isImmutable,assets"],
                                    ["api", "repos/owner/store-release/git/ref/tags/v1.2.3", "--jq", ".object.sha"]])
        extra = self.release(assets=[*self.release()["assets"], {"name": "notes.txt", "digest": "sha256:" + "1" * 64}])
        # The pypi job checks the files it downloaded; the release job checks the release is exactly its files.
        self.release_check(FakeGh(extra), complete=False)
        cases = {
            "complete": (FakeGh(extra), r"carries other assets than this run's: \['notes.txt'\]"),
            "draft": (FakeGh(self.release(isDraft=True)), "is not a published, immutable release"),
            "mutable": (FakeGh(self.release(isImmutable=False)), "is not a published, immutable release"),
            "tag": (FakeGh(self.release(), tagged="b" * 40), f"v1.2.3 points at {'b' * 40}, not {SHA}"),
            "digest": (FakeGh(self.release(assets=[{"name": self.sdist, "digest": "sha256:" + "0" * 64},
                                                   {"name": self.wheel, "digest": f"sha256:{self.digests[self.wheel]}"}])),
                       f"{self.sdist} is not this run's file"),
            "missing": (FakeGh(self.release(assets=[])), "are not this run's file"),
        }
        for label, (gh, error) in cases.items():
            with self.subTest(case=label), self.assertRaisesRegex(dists.DistError, error):
                self.release_check(gh)

    def test_a_release_not_complete_yet_is_checked_again(self) -> None:
        """Right after `gh release create`, GitHub may still show a draft, a mutable release or missing assets."""
        states = [self.release(isDraft=True), self.release(isImmutable=False), self.release(assets=[]), self.release()]
        slept, out = self.release_check(ChangingGh(states))
        self.assertEqual(slept, [dists.GH_INTERVAL] * 3)
        self.assertIn("v1.2.3 is not a published, immutable release (isDraft=True, isImmutable=True); trying again",
                      out)
        self.assertIn("are not this run's file (missing, or another SHA-256); trying again", out)
        self.assertIn(f"v1.2.3 is published and immutable on {SHA}, with exactly these 2 files", out)
        # What stays wrong fails once the attempts are spent.
        gh = FakeGh(self.release(isImmutable=False))
        slept = []
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(dists.DistError, "isImmutable=False"):
            dists.release_check(self.dist, "1.2.3", sha=SHA, repository="owner/store-release", gh=gh, attempts=3,
                                interval=5, sleep=slept.append)
        self.assertEqual((slept, len(gh.calls)), ([5, 5], 6))

    def test_reading_the_release_is_retried(self) -> None:
        slept, _ = self.release_check(FakeGh(self.release(), failures=2))
        self.assertEqual(slept, [dists.GH_INTERVAL, dists.GH_INTERVAL])
        with self.assertRaisesRegex(dists.DistError, r"cannot read the GitHub Release v1.2.3 \(gh release view failed: "
                                                     r"release not found\)"):
            self.release_check(FakeGh(None))
        for kwargs, error in (({"sha": "abc"}, "is not a full commit SHA"), ({"repository": "nope"}, "is not owner/name")):
            with self.subTest(**kwargs), self.assertRaisesRegex(dists.DistError, error):
                dists.release_check(self.dist, "1.2.3", **{"sha": SHA, "repository": "owner/store-release", **kwargs},
                                    gh=FakeGh(self.release()))

    def test_main_reports_errors(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = dists.main(["pypi-plan", str(self.tmp / "missing"), "--version", "1.2.3", "--upload-dir",
                               str(self.tmp / "upload")], opener=FakePypi())
        self.assertEqual(code, 1)
        self.assertRegex(err.getvalue(), r"^error: .*missing is not a folder")


def run_installed(argv: list[str], cwd: Path, prog: str = cli.CONSOLE) -> tuple[int, str, str]:
    """cli.main as the installed command would call it, from cwd, with the app configuration restored after."""
    out, err = io.StringIO(), io.StringIO()
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("IOS_", "ANDROID_", "STORE_", "GITHUB_"))}
    saved = support_apps.snapshot()
    try:
        with mock.patch.dict(os.environ, environ, clear=True), contextlib.chdir(cwd), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv, prog=prog)
    finally:
        support_apps.restore(saved)
    return code, out.getvalue(), err.getvalue()


class InstalledCommandTests(unittest.TestCase):
    """The store-release console script and python -m store_release: run.py's CLI, the app in the current directory."""

    def test_usage_names_how_the_command_was_started(self) -> None:
        self.assertTrue(cli.usage().startswith("usage: run.py [--app-root DIR] [--config PATH] <command>"))
        self.assertIn("(required)", cli.usage())
        installed = cli.usage(cli.CONSOLE)
        self.assertTrue(installed.startswith("usage: store-release [--app-root DIR] [--config PATH] <command>"))
        self.assertIn("(default: the current directory)", installed)
        self.assertEqual(cli.usage().split("commands:", 1)[1], installed.split("commands:", 1)[1])

    def test_the_installed_command_reads_the_app_in_the_current_directory(self) -> None:
        app = support_apps.default_app()
        code, out, err = run_installed(["config", "check"], app.root)
        self.assertEqual(code, 0, err)
        self.assertIn("Fud AI (ios, android)", out)
        # run.py never guesses: its working directory may be the tooling checkout.
        code, _, err = run_installed(["config", "check"], app.root, prog=cli.RUN_PY)
        self.assertEqual(code, 2)
        self.assertIn("pass --app-root DIR", err)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_installed(["--app-root", str(app.root), "config", "check"], Path(tmp))
        self.assertEqual(code, 0, err)

    def test_init_writes_into_the_current_directory_and_names_the_installed_command(self) -> None:
        args = [*scaffold.TEMPLATES["ios-and-android"], "--sha", scaffold.PLACEHOLDER_SHA, "--allow-untagged",
                "--no-input"]
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_installed(["init", *args], Path(tmp))
            self.assertEqual(code, 0, err)
            self.assertIn("Check the files: store-release config check --files", out)
            written = {p.relative_to(tmp).as_posix(): p.read_text(encoding="utf-8")
                       for p in sorted(Path(tmp).rglob("*")) if p.is_file()}
            self.assertEqual(written, dists.template_files("ios-and-android"))
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_installed(["--app-root", tmp, "init", *args], TOOL_ROOT, prog=cli.RUN_PY)
            self.assertEqual(code, 0, err)
            self.assertIn("Check the files: python3 -I -u <store-release>/run.py --app-root . config check --files", out)
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_installed(["init", *args], Path(tmp), prog=cli.MODULE)
            self.assertEqual(code, 0, err)
            self.assertIn("Check the files: python3 -m store_release config check --files", out)

    def test_an_unknown_command_fails_before_the_app_is_read(self) -> None:
        source = (TOOL_ROOT / "store_release" / "cli.py").read_text(encoding="utf-8")
        dispatched = set(re.findall(r'command == "([a-z-]+)"', source))
        for group in re.findall(r"command in \(([^)]*)\)", source):
            dispatched |= set(re.findall(r'"([a-z-]+)"', group))
        self.assertEqual(sorted(cli.COMMANDS), sorted(dispatched))
        with tempfile.TemporaryDirectory() as tmp:
            for prog in (cli.CONSOLE, cli.RUN_PY):
                code, _, err = run_installed(["bogus"], Path(tmp), prog=prog)
                with self.subTest(prog=prog):
                    self.assertEqual(code, 2)
                    self.assertTrue(err.startswith("error: unknown command 'bogus'\n\nusage: "), err)

    def test_python_dash_m_store_release(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        version = subprocess.run([sys.executable, "-m", "store_release", "--version"], cwd=TOOL_ROOT, env=env,
                                 capture_output=True, text=True)
        self.assertEqual((version.returncode, version.stdout), (0, f"store-release {__version__}\n"), version.stderr)
        usage = subprocess.run([sys.executable, "-m", "store_release", "bogus"], cwd=TOOL_ROOT, env=env,
                               capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)
        self.assertIn("usage: python3 -m store_release [--app-root DIR]", usage.stderr)

    def test_a_missing_extra_is_named_instead_of_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"jwt": None}):
            code, out, err = run_installed(["smoke"], Path(tmp))
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err, 'error: this command needs the ios extra (jwt is not installed): install '
                                  '"store-release[ios]", or "store-release[all]" for every command\n')
            code, _, err = run_installed(["smoke"], Path(tmp), prog=cli.RUN_PY)
            self.assertEqual(code, 1)
            self.assertEqual(err, "error: this command needs jwt, which is not installed: install the hash-locked "
                                  "requirements.txt and requirements-checks.txt\n")
        # Any other missing module is a bug, not a missing extra: it is not hidden.
        missing = ModuleNotFoundError("No module named 'store_release.nope'", name="store_release.nope")
        with mock.patch.object(smoke, "main", side_effect=missing), self.assertRaises(ModuleNotFoundError):
            run_installed(["smoke"], TOOL_ROOT)

    def test_every_third_party_module_names_its_extra(self) -> None:
        """cli.EXTRAS covers each third-party module the package imports, with an extra pyproject.toml has."""
        imported = set()
        for path in (TOOL_ROOT / "store_release").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.partition(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.partition(".")[0])
        third_party = imported - set(sys.stdlib_module_names) - {"store_release"}
        self.assertEqual(sorted(cli.EXTRAS), sorted(third_party))
        self.assertLessEqual(set(cli.EXTRAS.values()), set(PROJECT["optional-dependencies"]) - {"all"})

    def test_selftest_needs_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(selftest, "TOOL_ROOT", Path(tmp)), \
                mock.patch.object(selftest, "TESTS", Path(tmp) / "tests"):
            err = io.StringIO()
            with redirect_stderr(err):
                code = selftest.main([])
        self.assertEqual(code, 2)
        self.assertIn("an installed package without them", err.getvalue())


if __name__ == "__main__":
    unittest.main()
