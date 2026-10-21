"""store-release's own release.yml and CI's package job (PyYAML): which job builds the sdist and the wheel, which
signs and attests them, which publishes them to PyPI, with what permissions, and that the docs list the assets."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

TOOL_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = TOOL_ROOT / ".github" / "workflows"
PUBLISH = "pypa/gh-action-pypi-publish"
MAIN_ONLY = "github.ref == 'refs/heads/main'"
# Every release asset, in the order `gh release create` attaches them (X.Y.Z for ${VERSION}).
ASSETS = ("store-release-X.Y.Z-source.tar.gz", "store-release-X.Y.Z-source.tar.gz.sha256",
          "store-release-X.Y.Z-source.tar.gz.sigstore.json", "store-release-X.Y.Z-source.tar.gz.intoto.jsonl",
          "store_release-X.Y.Z.tar.gz", "store_release-X.Y.Z.tar.gz.sha256", "store_release-X.Y.Z.tar.gz.sigstore.json",
          "store_release-X.Y.Z-py3-none-any.whl", "store_release-X.Y.Z-py3-none-any.whl.sha256",
          "store_release-X.Y.Z-py3-none-any.whl.sigstore.json", "store_release-X.Y.Z.intoto.jsonl")


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True)
    return data


def scripts(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def step(job: dict, name: str) -> dict:
    (found,) = [s for s in job["steps"] if s.get("name") == name]
    return found


def uses(job: dict, action: str) -> list[dict]:
    return [s for s in job["steps"] if s.get("uses", "").startswith(f"{action}@")]


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = load("release.yml")
        self.jobs = self.data["jobs"]

    def test_dispatch_only_from_main_and_one_at_a_time(self) -> None:
        self.assertEqual(list(self.data["on"]), ["workflow_dispatch"])
        self.assertEqual(list(self.data["on"]["workflow_dispatch"]["inputs"]), ["version"])
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertEqual(self.data["concurrency"], {"group": "release", "cancel-in-progress": False})
        self.assertEqual(list(self.jobs), ["dists", "release", "pypi"])
        self.assertEqual([job["if"] for job in self.jobs.values()], [MAIN_ONLY] * 3)
        self.assertEqual((self.jobs["release"]["needs"], self.jobs["pypi"]["needs"]), ("dists", "release"))

    def test_each_job_has_only_the_permissions_it_needs(self) -> None:
        self.assertEqual(self.jobs["dists"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["release"]["permissions"], {"contents": "write", "checks": "read", "actions": "read",
                                                               "id-token": "write", "attestations": "write"})
        self.assertEqual(self.jobs["pypi"]["permissions"], {"contents": "read", "id-token": "write"})
        # PyPI's trusted publisher names the environment: only the publishing job runs in it.
        self.assertEqual(self.jobs["pypi"]["environment"],
                         {"name": "pypi", "url": "https://pypi.org/project/store-release/${{ inputs.version }}/"})
        self.assertEqual([job_id for job_id, job in self.jobs.items() if "environment" in job], ["pypi"])

    def test_pypi_gets_no_secret_or_token(self) -> None:
        self.assertNotIn("secrets.", (WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
        (publish,) = uses(self.jobs["pypi"], PUBLISH)
        self.assertEqual(publish["with"], {"packages-dir": "pypi-upload/", "attestations": True, "print-hash": True})
        self.assertEqual(publish["if"], "steps.plan.outputs.upload == 'true'")
        self.assertEqual([job_id for job_id, job in self.jobs.items() if uses(job, PUBLISH)], ["pypi"])

    def test_only_the_dists_job_builds(self) -> None:
        self.assertIn('scripts/dists.py build --out "$RUNNER_TEMP/python-dists" --version "$VERSION"',
                      scripts(self.jobs["dists"]))
        self.assertIn('scripts/dists.py check "$RUNNER_TEMP/python-dists"', scripts(self.jobs["dists"]))
        for job_id in ("release", "pypi"):
            with self.subTest(job=job_id):
                self.assertNotIn("dists.py build", scripts(self.jobs[job_id]))
                self.assertNotIn("pip install", scripts(self.jobs[job_id]))
        for job_id, job in self.jobs.items():
            for install in re.findall(r"pip install[^\n]*", scripts(job)):
                with self.subTest(job=job_id):
                    self.assertIn("--require-hashes -r requirements-dist.txt", install)

    def test_the_release_signs_attests_and_attaches_the_dists_built_from_this_commit(self) -> None:
        release, dists = self.jobs["release"], self.jobs["dists"]
        names = [s.get("name") or s["uses"].split("@")[0] for s in release["steps"]]
        (upload,) = uses(dists, "actions/upload-artifact")
        (download,) = uses(release, "actions/download-artifact")
        self.assertEqual(upload["with"]["name"], download["with"]["name"])
        self.assertEqual(download["with"]["path"], "${{ runner.temp }}/dist")
        order = ["Check the release preconditions", "actions/download-artifact", "Checksum the sdist and the wheel",
                 "Sign the source archive, the sdist and the wheel (Sigstore keyless)", "Attest build provenance",
                 "Attest the sdist's and the wheel's build provenance", "Add the provenance to the release assets",
                 "Create the tag and the GitHub Release"]
        self.assertEqual(sorted(order, key=names.index), order)
        attested = step(release, "Attest the sdist's and the wheel's build provenance")["with"]["subject-path"]
        self.assertEqual(attested.splitlines(), ["${{ runner.temp }}/dist/store_release-*.tar.gz",
                                            "${{ runner.temp }}/dist/store_release-*-py3-none-any.whl"])
        create = step(release, "Create the tag and the GitHub Release")["run"]
        attached = [name.replace("${VERSION}", "X.Y.Z") for name in re.findall(r'"\$dist/([^"]+)"', create)]
        self.assertEqual(tuple(attached), ASSETS)
        # Whatever gh reports, the release counts only when it is exactly this run's.
        self.assertIn('if ! gh release create "v$VERSION"', create)
        self.assertTrue(create.rstrip().endswith('python3 -I scripts/dists.py release-check "$dist" --version "$VERSION" '
                                                 '--sha "$GITHUB_SHA" \\\n  --repository "$GITHUB_REPOSITORY" --complete'),
                        create)

    def test_pypi_publishes_the_releases_own_files_and_checks_the_result(self) -> None:
        pypi = self.jobs["pypi"]
        names = [s.get("name") for s in pypi["steps"] if s.get("name")]
        self.assertEqual(names, ["Download the release's sdist and wheel, and check them", "Compare them with what PyPI has",
                                 "Publish to PyPI", "Check that PyPI serves the release's files, attested by this workflow"])
        download = step(pypi, "Download the release's sdist and wheel, and check them")["run"]
        for fragment in ('gh release download "v$VERSION" --repo "$GITHUB_REPOSITORY" --dir "$release"',
                         '--pattern "store_release-${VERSION}*"',
                         'release-check "$release" --version "$VERSION" --sha "$GITHUB_SHA"',
                         'sha256sum -c "$file.sha256"',
                         'gh attestation verify "$file" --repo "$GITHUB_REPOSITORY" '
                         '--bundle "store_release-${VERSION}.intoto.jsonl"',
                         '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release.yml"',
                         '--source-ref refs/heads/main --source-digest "$GITHUB_SHA" --deny-self-hosted-runners'):
            self.assertIn(fragment, download)
        self.assertNotIn("--complete", download)
        plan = step(pypi, "Compare them with what PyPI has")
        self.assertEqual(plan["id"], "plan")
        self.assertIn('pypi-plan "$RUNNER_TEMP/dists" --version "$VERSION" --upload-dir pypi-upload', plan["run"])
        verify = step(pypi, "Check that PyPI serves the release's files, attested by this workflow")["run"]
        self.assertIn('--repository "$GITHUB_REPOSITORY" --workflow release.yml --environment pypi', verify)
        self.assertNotIn("if", step(pypi, "Check that PyPI serves the release's files, attested by this workflow"))

    def test_expressions_reach_shell_only_through_env(self) -> None:
        for job_id, job in self.jobs.items():
            for found in job["steps"]:
                if "run" in found:
                    with self.subTest(job=job_id, step=found.get("name")):
                        self.assertNotIn("${{", found["run"])

    def test_the_docs_list_every_asset(self) -> None:
        for name in ("docs/release-process.md", "docs/verification.md"):
            text = (TOOL_ROOT / name).read_text(encoding="utf-8")
            rows = re.findall(r"(?m)^\| `([^`]+)` \|", text)
            with self.subTest(doc=name):
                self.assertEqual(tuple(rows), ASSETS)


class PackageJobTests(unittest.TestCase):
    def test_ci_builds_checks_and_smoke_tests_the_package(self) -> None:
        job = load("ci.yml")["jobs"]["package"]
        self.assertEqual(job["name"], "package")
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertNotIn("if", job)
        run = scripts(job)
        self.assertIn("python3 -m pip install --disable-pip-version-check --require-hashes -r requirements-dist.txt", run)
        self.assertIn('python3 -I scripts/dists.py build --out "$RUNNER_TEMP/dist"', run)
        self.assertIn('python3 -I scripts/dists.py check "$RUNNER_TEMP/dist"', run)
        (setup,) = uses(job, "actions/setup-python")
        versions = setup["with"]["python-version"].split()
        self.assertEqual(versions[-1], "3.13", "python3, which builds, is the version the other CI jobs use")
        self.assertEqual(sorted(re.findall(r"--python python(3\.\d+)", run)), sorted(versions))


if __name__ == "__main__":
    unittest.main()
