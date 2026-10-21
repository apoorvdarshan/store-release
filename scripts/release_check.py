#!/usr/bin/env python3
"""store-release's own release preconditions (release.yml); not part of the package.

    python3 -I scripts/release_check.py --version X.Y.Z --sha SHA --notes-out FILE

Fails (exit 1, `error: ...` on stderr) unless every one of these holds:

1. GITHUB_REF is refs/heads/main.
2. VERSION is X.Y.Z and equals store_release.__version__ (read from
   store_release/__init__.py by AST, never imported).
3. No tag vVERSION exists, and VERSION is greater than every existing vX.Y.Z tag.
4. CHANGELOG.md has exactly one "## [VERSION] - YYYY-MM-DD" section whose body is
   non-empty and has an "Impact: checks-only|validation|release-path|config-breaking"
   line. That body is written to --notes-out (the GitHub Release body).
5. docs/yanked.md does not list VERSION.
6. On --sha, the newest check run of each required check concluded success. Only
   check runs that GitHub Actions created (app id 15368) in this commit's ci.yml or
   codeql.yml workflow runs count, so a run of the same name from another app or
   workflow cannot stand in. A run still queued or in progress counts as a failure:
   wait, then dispatch again.

Reads GitHub with the gh CLI (GH_TOKEN, GITHUB_REPOSITORY). Writes nothing but
--notes-out; release.yml creates the tag only after this passes.
"""
from __future__ import annotations

import argparse
import ast
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CHECKS = ("test (stdlib)", "test (strict)", "test (macos)", "lint", "package", "workflows",
                   "Analyze (python)", "Analyze (actions)")
# Only GitHub Actions check runs from these workflows count (the required checks are their jobs).
GITHUB_ACTIONS_APP_ID = 15368
REQUIRED_WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/codeql.yml")
IMPACTS = ("checks-only", "validation", "release-path", "config-breaking")
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\S+))?\s*$")
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]: \S+")
IMPACT_LINE = re.compile(rf"^Impact: ({'|'.join(IMPACTS)})\s*$", re.MULTILINE)
YANKED = re.compile(r"^- v(\d+\.\d+\.\d+)\b", re.MULTILINE)
TAG = re.compile(r"^refs/tags/v(\d+)\.(\d+)\.(\d+)$")

Gh = Callable[[Sequence[str]], str]


class ReleaseCheckError(Exception):
    """A release precondition does not hold."""


def run_gh(args: Sequence[str]) -> str:
    """`gh <args>` stdout; a non-zero exit is an error (the token stays in GH_TOKEN, never in argv)."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReleaseCheckError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def package_version(init_file: Path) -> str:
    tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
            raise ReleaseCheckError(f"{init_file.name}: __version__ is not a string literal")
    raise ReleaseCheckError(f"{init_file.name} has no __version__")


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(text or "")
    if not match:
        raise ReleaseCheckError(f"version {text!r} is not X.Y.Z")
    return int(match[1]), int(match[2]), int(match[3])


def changelog_notes(text: str, version: str) -> str:
    """The body of the version's section; fails unless there is exactly one, dated, with an Impact line."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if (m := SECTION.match(line)) and m["version"] == version]
    if not starts:
        raise ReleaseCheckError(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section")
    if len(starts) > 1:
        raise ReleaseCheckError(f"CHANGELOG.md has {len(starts)} sections for {version}")
    start = starts[0]
    date = SECTION.match(lines[start])["date"]
    try:
        if date is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ReleaseCheckError(f"CHANGELOG.md: the [{version}] section needs a ' - YYYY-MM-DD' date") from None
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## ") or LINK_DEFINITION.match(line):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise ReleaseCheckError(f"CHANGELOG.md: the [{version}] section is empty")
    if not IMPACT_LINE.search(notes):
        raise ReleaseCheckError(f"CHANGELOG.md: the [{version}] section has no 'Impact: {'|'.join(IMPACTS)}' line")
    return notes + "\n"


def yanked_versions(text: str) -> set[str]:
    return set(YANKED.findall(text))


def check_tags(gh: Gh, repository: str, version: str) -> None:
    refs = json.loads(gh(["api", f"repos/{repository}/git/matching-refs/tags/v"]) or "[]")
    wanted = parse_version(version)
    existing = []
    for ref in refs:
        name = ref.get("ref", "")
        if name == f"refs/tags/v{version}":
            raise ReleaseCheckError(f"tag v{version} already exists; tags never move, so release the next patch")
        if match := TAG.match(name):
            existing.append((int(match[1]), int(match[2]), int(match[3])))
    newer = [v for v in existing if v >= wanted]
    if newer:
        highest = ".".join(map(str, max(newer)))
        raise ReleaseCheckError(f"v{version} is not greater than the existing tag v{highest}")


def workflow_suites(gh: Gh, repository: str, sha: str) -> set[int]:
    """The check suites of this commit's ci.yml and codeql.yml workflow runs."""
    data = json.loads(gh(["api", f"repos/{repository}/actions/runs?head_sha={sha}&per_page=100"]))
    return {run["check_suite_id"] for run in data.get("workflow_runs", [])
            if run.get("path") in REQUIRED_WORKFLOWS and isinstance(run.get("check_suite_id"), int)}


def check_runs(gh: Gh, repository: str, sha: str) -> None:
    suites = workflow_suites(gh, repository, sha)
    data = json.loads(gh(["api", f"repos/{repository}/commits/{sha}/check-runs?per_page=100"]))
    newest: dict[str, dict] = {}
    for run in data.get("check_runs", []):
        name = run.get("name")
        if (name not in REQUIRED_CHECKS or (run.get("app") or {}).get("id") != GITHUB_ACTIONS_APP_ID
                or (run.get("check_suite") or {}).get("id") not in suites):
            continue
        if name not in newest or run.get("id", 0) > newest[name].get("id", 0):
            newest[name] = run
    problems = []
    for name in REQUIRED_CHECKS:
        run = newest.get(name)
        if run is None:
            problems.append(f"{name}: no check run from {' or '.join(REQUIRED_WORKFLOWS)} on {sha[:12]}")
        elif run.get("status") != "completed":
            problems.append(f"{name}: still {run.get('status')}; wait for it, then dispatch again")
        elif run.get("conclusion") != "success":
            problems.append(f"{name}: {run.get('conclusion')}")
    if problems:
        raise ReleaseCheckError("required checks did not pass:\n  " + "\n  ".join(problems))


def check(*, version: str, sha: str, root: Path, environ: Mapping[str, str], gh: Gh) -> str:
    """Every precondition, in order; returns the release notes."""
    if environ.get("GITHUB_REF") != "refs/heads/main":
        raise ReleaseCheckError(f"releases run from refs/heads/main, not {environ.get('GITHUB_REF') or 'an unknown ref'}")
    parse_version(version)
    current = package_version(root / "store_release" / "__init__.py")
    if current != version:
        raise ReleaseCheckError(f"store_release.__version__ is {current!r}, not {version!r}")
    if not SHA.fullmatch(sha or ""):
        raise ReleaseCheckError(f"--sha {sha!r} is not a full commit SHA")
    repository = environ.get("GITHUB_REPOSITORY", "")
    if not REPOSITORY.fullmatch(repository):
        raise ReleaseCheckError(f"GITHUB_REPOSITORY {repository!r} is not owner/name")
    check_tags(gh, repository, version)
    notes = changelog_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if version in yanked_versions((root / "docs" / "yanked.md").read_text(encoding="utf-8")):
        raise ReleaseCheckError(f"docs/yanked.md lists {version}")
    check_runs(gh, repository, sha)
    return notes


def main(argv: list[str] | None = None, *, gh: Gh = run_gh, environ: Mapping[str, str] | None = None,
         root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--notes-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        notes = check(version=args.version, sha=args.sha, root=root,
                      environ=os.environ if environ is None else environ, gh=gh)
    except (ReleaseCheckError, OSError, ValueError, SyntaxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.notes_out.write_text(notes, encoding="utf-8")
    print(f"release v{args.version}: every precondition holds ({', '.join(REQUIRED_CHECKS)} passed on {args.sha[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
