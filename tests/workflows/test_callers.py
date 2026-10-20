"""The callers: rendering reproduces examples/fud-ai/callers byte for byte, and the pull request
check (PyYAML) catches every single-key change, forbidden job names, secret mappings and pins."""
from __future__ import annotations

import copy
import io
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from store_release import __version__, callers, config
from support import apps as support_apps
from support.tag_fakes import TagApi

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = support_apps.TOOL_ROOT
EXAMPLES = TOOL_ROOT / "examples"
ZERO = "0" * 40
SHA = "f" * 40
FILES = {"ios": "ios-release.yml", "android": "android-release.yml", "checks": "store-checks.yml"}


def fud_ai() -> config.Config:
    return config.load(EXAMPLES / "fud-ai" / "store-release.toml")


def render(platform: str, cfg: config.Config | None = None, sha: str = ZERO, version: str = "0.1.0") -> str:
    return callers.render(cfg or fud_ai(), platform, sha=sha, version=version, app_repository="apoorvdarshan/fud-ai")


def leaves(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, path + (index,))
    else:
        yield path, value


def mutate(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if value is None:
        return "changed"
    return f"{value}x"


class RenderTests(unittest.TestCase):
    def test_the_examples_are_the_renderer_output(self) -> None:
        for platform, name in FILES.items():
            with self.subTest(platform=platform):
                self.assertEqual(render(platform), (EXAMPLES / "fud-ai" / "callers" / name).read_text())

    def test_the_rendered_callers_are_pinned_and_valid_yaml(self) -> None:
        for platform in FILES:
            text = render(platform, sha=SHA, version="0.1.0")
            data = yaml.safe_load(text)
            self.assertIn(True, data, "the on: key")
            for _, workflow, ref, rest in callers.store_release_refs(text, "apoorvdarshan/store-release"):
                self.assertEqual((ref, rest.strip()), (SHA, "# v0.1.0"), workflow)
            for job in data["jobs"]:
                self.assertNotIn(job, callers.FORBIDDEN_JOB_NAMES)

    def test_the_example_app_renders(self) -> None:
        example = config.load(EXAMPLES / "example" / "store-release.toml")
        ios = yaml.safe_load(render("ios", example))
        self.assertEqual(ios[True]["push"]["tags"], ["v*"])
        self.assertEqual(ios["jobs"]["build"]["needs"], ["tests", "prepare"])
        self.assertIn("needs.tests.result == 'success'", ios["jobs"]["build"]["if"])
        android = render("android", example)
        self.assertIn("example-android-X.Y-N.apk", android)
        self.assertEqual(yaml.safe_load(android)["jobs"]["release"]["needs"], ["gates", "tests"])
        checks = yaml.safe_load(render("checks", example))
        self.assertNotIn("paths", checks[True]["push"])

    def test_a_non_default_config_is_passed_to_every_store_release_job(self) -> None:
        text = callers.render(fud_ai(), "ios", sha=ZERO, version="0.1.0", config_path="ci/release.toml")
        jobs = yaml.safe_load(text)["jobs"]
        for name in ("prepare", "build", "wait-for-build", "app-store-connect", "github-release", "store-plan"):
            self.assertEqual(jobs[name]["with"]["config"], "ci/release.toml", name)
        self.assertNotIn("with", jobs["ios-tests"])

    def test_bad_render_input(self) -> None:
        for kwargs in ({"sha": "abc", "version": "0.1.0"}, {"sha": ZERO, "version": "v0.1"},
                       {"sha": ZERO, "version": "0.1.0", "repository": "x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(callers.CallerError):
                callers.render(fud_ai(), "ios", **kwargs)
        with self.assertRaisesRegex(callers.CallerError, "app repository"):
            callers.render(fud_ai(), "android", sha=ZERO, version="0.1.0")


class PullRequestCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "app"
        self.workflows = self.root / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        self.cfg = fud_ai()
        self.env = {"TOOL_SHA": SHA, "TOOL_REPOSITORY": "apoorvdarshan/store-release",
                    "GITHUB_REPOSITORY": "apoorvdarshan/fud-ai"}
        self.tags = TagApi({f"v{__version__}": SHA})
        for platform, name in FILES.items():
            (self.workflows / name).write_text(render(platform, sha=SHA, version=__version__))

    def check(self) -> None:
        with redirect_stdout(io.StringIO()):
            callers.pr_check(self.cfg, self.root, environ=self.env, opener=self.tags)

    def pins(self) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            callers.pins_check(self.root, environ=self.env, opener=self.tags)
        return out.getvalue()

    def fails(self, fragment: str) -> None:
        with self.assertRaises(callers.CallerError) as caught:
            self.check()
        self.assertIn(fragment, str(caught.exception))

    def test_the_rendered_callers_pass(self) -> None:
        self.check()
        self.assertIn(f"the v{__version__} tag points at {SHA}", self.pins())

    def test_the_release_tag_must_point_at_the_pin(self) -> None:
        cases = {
            "untagged": (TagApi({}), f"has no release tag v{__version__}"),
            "another commit": (TagApi({f"v{__version__}": ZERO}), f"v{__version__} is {ZERO}, not the pinned {SHA}"),
            "unreadable": (TagApi(error=urllib.error.URLError("offline")), "could not read"),
        }
        for name, (tags, fragment) in cases.items():
            self.tags = tags
            with self.subTest(case=name, check="pr"):
                self.fails(fragment)
            with self.subTest(case=name, check="pins"), self.assertRaisesRegex(callers.CallerError, fragment):
                self.pins()
        self.tags = TagApi({f"v{__version__}": SHA}, annotated=True)
        self.check()
        self.pins()

    def test_every_single_key_change_is_reported(self) -> None:
        for platform, name in FILES.items():
            expected = callers._normalize(yaml.safe_load(render(platform, sha=SHA, version=__version__)))
            for path, value in leaves(expected):
                changed = copy.deepcopy(expected)
                node = changed
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = mutate(value)
                with self.subTest(file=name, path=path):
                    self.assertTrue(callers.differences(changed, expected))
                removed = copy.deepcopy(expected)
                node = removed
                for key in path[:-1]:
                    node = node[key]
                del node[path[-1]]
                with self.subTest(file=name, path=path, removed=True):
                    self.assertTrue(callers.differences(removed, expected))

    def test_textual_changes_fail_the_whole_check(self) -> None:
        ios = self.workflows / "ios-release.yml"
        original = ios.read_text()
        cases = [
            (original.replace("timeout-minutes", "timeout-minutes") + "\n  extra:\n    uses: ./x.yml\n", "jobs.extra"),
            (original.replace("needs.quality.result == 'success') ||", "needs.quality.result != 'skipped') ||"),
             "jobs.build.if"),
            (original.replace("      IOS_DEV_SIGNING_P12_PASSWORD: ${{ secrets.IOS_DEV_SIGNING_P12_PASSWORD }}\n", ""),
             "IOS_DEV_SIGNING_P12_PASSWORD: missing"),
            (original.replace("    secrets:\n      APP_STORE_CONNECT_API_KEY_ID: ${{ secrets.APP_STORE_CONNECT_API_KEY_ID }}\n",
                              "    secrets:\n      EXTRA: ${{ secrets.EXTRA }}\n"
                              "      APP_STORE_CONNECT_API_KEY_ID: ${{ secrets.APP_STORE_CONNECT_API_KEY_ID }}\n", 1),
             "EXTRA: not expected"),
            (original.replace(f"ios-build.yml@{SHA} # v{__version__}", f"ios-build.yml@{SHA} # v9.9.9"), "# v9.9.9"),
            (original.replace("  queue: max\n", ""), "concurrency.queue: missing"),
        ]
        for text, fragment in cases:
            ios.write_text(text)
            with self.subTest(fragment=fragment):
                self.fails(fragment)
        ios.write_text(original + "\n  web:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n")
        self.fails("a job named 'web'")
        ios.write_text(original)
        (self.workflows / "store-checks.yml").unlink()
        self.fails("store-checks.yml: missing")

    def test_pins_check_catches_a_half_upgrade(self) -> None:
        other = self.workflows / "other.yml"
        other.write_text("jobs:\n  x:\n    uses: apoorvdarshan/store-release/.github/workflows/checks.yml"
                         f"@{'1' * 40} # v{__version__}\n")
        with self.assertRaisesRegex(callers.CallerError, "other.yml line 3"):
            self.pins()
        other.write_text("jobs:\n  x:\n    uses: apoorvdarshan/store-release/.github/workflows/checks.yml"
                         f"@{SHA} # v0.0.9\n")
        with self.assertRaises(callers.CallerError):
            self.pins()
        self.assertEqual(self.tags.requests, [], "the tag is read only once the pins agree")

    def test_the_on_key_is_normalized(self) -> None:
        data = yaml.safe_load("on:\n  push: {}\n")
        self.assertEqual(callers._normalize(data), {"on": {"push": {}}})


class ExampleCopiesTests(unittest.TestCase):
    def test_examples_pass_caller_check_against_themselves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github").mkdir()
            shutil.copytree(EXAMPLES / "fud-ai" / "callers", root / ".github" / "workflows")
            for name in FILES.values():
                path = root / ".github" / "workflows" / name
                path.write_text(path.read_text().replace(ZERO, SHA).replace("# v0.1.0", f"# v{__version__}"))
            env = {"TOOL_SHA": SHA, "TOOL_REPOSITORY": "apoorvdarshan/store-release",
                   "GITHUB_REPOSITORY": "apoorvdarshan/fud-ai"}
            with redirect_stdout(io.StringIO()):
                callers.pr_check(fud_ai(), root, environ=env, opener=TagApi({f"v{__version__}": SHA}))


class RenderCommandTests(unittest.TestCase):
    """callers render renders only a release tag's commit, unless --allow-untagged (store-release's CI)."""

    def run_render(self, *extra: str, tags: TagApi) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--platform", "checks", "--sha", SHA, "--version", "0.1.0", *extra]
        with mock.patch("urllib.request.urlopen", tags), redirect_stdout(out), redirect_stderr(err):
            try:
                code = callers.render_main(fud_ai(), argv)
            except callers.CallerError as exc:
                return 1, out.getvalue(), str(exc)
        return code, out.getvalue(), err.getvalue()

    def test_a_tagged_commit_renders(self) -> None:
        code, out, err = self.run_render(tags=TagApi({"v0.1.0": SHA}))
        self.assertEqual(code, 0)
        self.assertIn(f"checks.yml@{SHA} # v0.1.0", out)
        self.assertIn(f"the v0.1.0 tag points at {SHA}", err)

    def test_an_untagged_commit_needs_allow_untagged(self) -> None:
        for tags, fragment in ((TagApi({}), "has no release tag v0.1.0"),
                               (TagApi({"v0.1.0": ZERO}), f"v0.1.0 is {ZERO}, not the pinned {SHA}")):
            with self.subTest(fragment=fragment):
                code, out, err = self.run_render(tags=tags)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(fragment, err)
        tags = TagApi({})
        code, out, _ = self.run_render("--allow-untagged", tags=tags)
        self.assertEqual(code, 0)
        self.assertIn(f"checks.yml@{SHA} # v0.1.0", out)
        self.assertEqual(tags.requests, [])


if __name__ == "__main__":
    unittest.main()
