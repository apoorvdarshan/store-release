#!/usr/bin/env python3
"""store-release's sdist and wheel (CI's package job, release.yml); not part of the package.

    python3 -I scripts/dists.py build --out DIR [--version X.Y.Z]
    python3 -I scripts/dists.py check DIR [--python PYTHON ...]
    python3 -I scripts/dists.py release-check DIR --version X.Y.Z --sha SHA --repository OWNER/NAME [--complete]
    python3 -I scripts/dists.py pypi-plan DIR --version X.Y.Z --upload-dir DIR
    python3 -I scripts/dists.py pypi-verify DIR --version X.Y.Z --repository OWNER/NAME

build        `python -m build --no-isolation`: the sdist, then the wheel from the sdist, with the
             tools requirements-dist.txt locks (installed in this Python) and SOURCE_DATE_EPOCH
             set to the commit's time, so rebuilding a commit gives the same files. DIR must be
             new or empty, and ends up with exactly store_release-<version>.tar.gz and
             store_release-<version>-py3-none-any.whl, <version> being store_release.__version__
             (which --version, when given, must equal).
check        `twine check --strict`; the sdist and the wheel hold exactly the expected files; the
             metadata names the version, the license, the Python floor and the console script,
             and the description has no relative link. Then, for each --python (default: this
             one), a new virtual environment gets the wheel alone (no dependencies, no index),
             and from empty directories outside the checkout: `store-release --version` and
             `--help`, `python -m store_release --version`, every module imports, `selftest`
             refuses, `smoke` names the extra it lacks (an `error:` line, not a traceback),
             and `store-release init` (no --app-root: the current directory) writes
             exactly examples/templates/<template>/ for each template, which `config check`
             then loads.
release-check
             (release.yml, with gh and GH_TOKEN) the GitHub Release vX.Y.Z is published and
             immutable, its tag is --sha, and each file in DIR is one of its assets with the
             same SHA-256; with --complete, its assets are exactly DIR's files. The release is
             read and checked again for a minute before that fails, since it may run right
             after `gh release create`.
pypi-plan    (release.yml, before the upload) compares DIR's two files with what PyPI lists for
             the version. A file PyPI has with the same SHA-256 is already published and is
             skipped; one with another digest, or a file of the version that this release did not
             build, fails; the rest are copied to --upload-dir, which must be new or empty.
             Writes upload=true|false to $GITHUB_OUTPUT when it is set.
pypi-verify  (release.yml, after the upload) waits until PyPI lists both files with DIR's
             digests, each with a PEP 740 attestation published by --repository's
             --workflow; it warns when that publisher does not name --environment.

Exit codes: 0 success, 1 a check failed (`error: ...` on stderr), 2 a usage error.
"""
from __future__ import annotations

import argparse
import ast
import email.parser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "store-release"
# The distribution name in file names (PEP 625 / the wheel spec): the project name, normalized.
DIST = "store_release"
PYPI = "https://pypi.org"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# What the sdist holds besides store_release/: pyproject.toml's sdist include list, PKG-INFO, and the
# .gitignore hatchling adds so that a build from the sdist excludes what the checkout excludes.
SDIST_FILES = ("PKG-INFO", "pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "hatch_build.py", ".gitignore")
WHEEL_INFO_FILES = ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE")
CONSOLE_SCRIPT = "store-release = store_release.cli:console_main"
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
ABSOLUTE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|#)")
TEMPLATES = ROOT / "examples" / "templates"
HTTP_TIMEOUT = 30
GH_ATTEMPTS = 6
GH_INTERVAL = 10
VERIFY_TIMEOUT = 600
VERIFY_INTERVAL = 15
# The module list, imported in the new environment: every module must import with the standard library alone.
IMPORT_EVERY_MODULE = """
import importlib, pkgutil, store_release
names = sorted(m.name for m in pkgutil.walk_packages(store_release.__path__, "store_release."))
for name in names:
    importlib.import_module(name)
print(len(names))
"""
TEMPLATE_ANSWERS = """
import json
from store_release import scaffold
print(json.dumps({"sha": scaffold.PLACEHOLDER_SHA, "templates": scaffold.TEMPLATES}))
"""

Opener = Callable[..., object]
Gh = Callable[[list[str]], str]


class DistError(Exception):
    """A distribution is missing, malformed or does not match what the release built."""


class PypiMismatch(DistError):
    """PyPI has a file of the version that differs from this release's (waiting does not help)."""


# --- shared -------------------------------------------------------------------------


def package_version(root: Path = ROOT) -> str:
    """store_release.__version__, read from the source by AST (never imported)."""
    init = root / "store_release" / "__init__.py"
    for node in ast.parse(init.read_text(encoding="utf-8"), filename=str(init)).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise DistError(f"{init} has no __version__ string")


def dist_names(version: str) -> tuple[str, str]:
    """(sdist, wheel) file names for a version."""
    if not VERSION.fullmatch(version or ""):
        raise DistError(f"version {version!r} is not X.Y.Z")
    return f"{DIST}-{version}.tar.gz", f"{DIST}-{version}-py3-none-any.whl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def the_dists(dist_dir: Path, version: str) -> dict[str, Path]:
    """{name: path} of the version's two files, which must be all that dist_dir holds."""
    if not dist_dir.is_dir():
        raise DistError(f"{dist_dir} is not a folder")
    names = dist_names(version)
    present = sorted(p.name for p in dist_dir.iterdir())
    if present != sorted(names):
        raise DistError(f"{dist_dir} holds {present}, not exactly {sorted(names)}")
    return {name: dist_dir / name for name in names}


def empty_dir(path: Path, option: str) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise DistError(f"{option} {path} must be a new or empty folder")
    path.mkdir(parents=True, exist_ok=True)


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise DistError(f"`{' '.join(str(a) for a in args)}` exited {result.returncode}:\n"
                        f"{result.stdout}{result.stderr}".rstrip())
    return result


def tracked_package_files(root: Path = ROOT) -> list[str]:
    """Every file git tracks under store_release/ (what the wheel and the sdist must carry)."""
    result = run(["git", "-C", str(root), "ls-files", "-z", "--", "store_release"])
    files = sorted(name for name in result.stdout.split("\0") if name)
    if not files:
        raise DistError("git ls-files found no store_release/ files")
    return files


# --- build --------------------------------------------------------------------------


def build(out: Path, *, version: str | None = None, root: Path = ROOT) -> list[Path]:
    current = package_version(root)
    if version is not None and version != current:
        raise DistError(f"store_release.__version__ is {current!r}, not {version!r}")
    empty_dir(out, "--out")
    epoch = run(["git", "-C", str(root), "log", "-1", "--format=%ct"]).stdout.strip()
    if not epoch.isdigit():
        raise DistError(f"cannot read the commit time ({epoch!r})")
    env = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
    # -I: the build frontend never imports from the working directory.
    result = run([sys.executable, "-I", "-m", "build", "--no-isolation", "--outdir", str(out), str(root)], env=env)
    print(f"{result.stdout}{result.stderr}".strip())
    dists = the_dists(out, current)
    for name, path in dists.items():
        print(f"{name}  sha256 {sha256(path)}")
    return list(dists.values())


# --- check --------------------------------------------------------------------------


def wheel_files(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return sorted(info.filename for info in archive.infolist() if not info.is_dir())


def sdist_files(sdist: Path, version: str) -> list[str]:
    prefix = f"{DIST}-{version}/"
    names = []
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(prefix):
                raise DistError(f"{sdist.name}: unexpected member {member.name!r}")
            names.append(member.name[len(prefix):])
    return sorted(names)


def read_member(archive_path: Path, member: str) -> str:
    if archive_path.suffix == ".whl":
        with zipfile.ZipFile(archive_path) as archive:
            return archive.read(member).decode("utf-8")
    with tarfile.open(archive_path, "r:gz") as archive:
        handle = archive.extractfile(member)
        if handle is None:
            raise DistError(f"{archive_path.name}: {member} is not a file")
        return handle.read().decode("utf-8")


def relative_links(text: str) -> list[str]:
    return [target for target in LINK_TARGET.findall(text) if not ABSOLUTE.match(target)]


def check_metadata(text: str, version: str, label: str, root: Path = ROOT) -> None:
    """The core metadata (METADATA or PKG-INFO) of a built file against pyproject.toml."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    message = email.parser.Parser().parsestr(text)
    expected = {"Name": PROJECT, "Version": version, "Requires-Python": project["requires-python"],
                "License-Expression": project["license"], "Description-Content-Type": "text/markdown"}
    for field, value in expected.items():
        if message.get(field) != value:
            raise DistError(f"{label}: {field} is {message.get(field)!r}, not {value!r}")
    extras = sorted(message.get_all("Provides-Extra") or [])
    if extras != sorted(project["optional-dependencies"]):
        raise DistError(f"{label}: Provides-Extra is {extras}, not {sorted(project['optional-dependencies'])}")
    if message.get_all("Requires-Dist") and any("extra ==" not in r for r in message.get_all("Requires-Dist")):
        raise DistError(f"{label}: a dependency outside the extras: the package needs only the standard library")
    description = message.get_payload()
    if "store-release" not in description[:200]:
        raise DistError(f"{label}: the description is not the README")
    if links := relative_links(description):
        raise DistError(f"{label}: the description has relative links, which do not resolve on PyPI: {links}")


def check_contents(dists: dict[str, Path], version: str, root: Path = ROOT) -> None:
    sdist_name, wheel_name = dist_names(version)
    package = tracked_package_files(root)
    info = f"{DIST}-{version}.dist-info"
    expected_wheel = sorted(package + [f"{info}/{name}" for name in WHEEL_INFO_FILES])
    found = wheel_files(dists[wheel_name])
    if found != expected_wheel:
        raise DistError(f"{wheel_name}: missing {sorted(set(expected_wheel) - set(found))}, "
                        f"unexpected {sorted(set(found) - set(expected_wheel))}")
    expected_sdist = sorted(package + list(SDIST_FILES))
    found = sdist_files(dists[sdist_name], version)
    if found != expected_sdist:
        raise DistError(f"{sdist_name}: missing {sorted(set(expected_sdist) - set(found))}, "
                        f"unexpected {sorted(set(found) - set(expected_sdist))}")
    check_metadata(read_member(dists[wheel_name], f"{info}/METADATA"), version, wheel_name, root)
    check_metadata(read_member(dists[sdist_name], f"{DIST}-{version}/PKG-INFO"), version, sdist_name, root)
    entry_points = read_member(dists[wheel_name], f"{info}/entry_points.txt")
    if CONSOLE_SCRIPT not in entry_points.splitlines():
        raise DistError(f"{wheel_name}: no console script `{CONSOLE_SCRIPT}`")
    print(f"contents: {len(package)} package files in both, the metadata and the console script")


def template_files(name: str) -> dict[str, str]:
    """{path init writes, relative to the app root: text} for examples/templates/<name>/."""
    folder = TEMPLATES / name
    out = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = path.relative_to(folder).as_posix()
        if relative == "store-release.toml":
            target = "store/store-release.toml"
        elif relative == "dependabot.yml":
            target = ".github/dependabot.yml"
        elif relative.startswith("callers/"):
            target = f".github/workflows/{relative[len('callers/'):]}"
        else:
            raise DistError(f"examples/templates/{name}/{relative}: not a file init writes")
        out[target] = path.read_text(encoding="utf-8")
    return out


def written_files(app: Path) -> dict[str, str]:
    return {p.relative_to(app).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(app.rglob("*")) if p.is_file()}


def smoke(python: str, wheel: Path, version: str) -> None:
    """The wheel alone in a new virtual environment, run from empty directories outside the checkout."""
    with tempfile.TemporaryDirectory(prefix="store-release-smoke-") as tmp:
        base = Path(tmp)
        venv = base / "venv"
        run([python, "-m", "venv", venv])
        bin_dir = venv / "bin"
        vpython, command = bin_dir / "python", bin_dir / "store-release"
        run([vpython, "-I", "-m", "pip", "install", "--no-deps", "--no-index", "--disable-pip-version-check",
             "--quiet", wheel])
        label = run([vpython, "-I", "-c", "import sys; print(sys.version.split()[0])"]).stdout.strip()
        work = base / "work"
        work.mkdir()
        # A clean environment: no PYTHONPATH, token or switch leaks in from the job.
        env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", HOME=str(base))

        def cli(*args: object, cwd: Path = work, check: bool = True) -> subprocess.CompletedProcess:
            return run([command, *args], cwd=cwd, env=env, check=check)

        expected = f"store-release {version}\n"
        if (out := cli("--version").stdout) != expected:
            raise DistError(f"python {label}: store-release --version printed {out!r}, not {expected!r}")
        module = run([vpython, "-I", "-m", "store_release", "--version"], cwd=work, env=env).stdout
        if module != expected:
            raise DistError(f"python {label}: python -m store_release --version printed {module!r}")
        usage = cli("--help").stdout
        if not usage.startswith("usage: store-release [--app-root DIR]") or "default: the current directory" not in usage:
            raise DistError(f"python {label}: store-release --help is not the installed command's usage:\n{usage}")
        location = run([vpython, "-I", "-c", "import store_release; print(store_release.__file__)"], cwd=work,
                       env=env).stdout.strip()
        if not Path(location).resolve().is_relative_to(venv.resolve()):
            raise DistError(f"python {label}: store_release imported from {location}, not the new environment")
        modules = run([vpython, "-I", "-c", IMPORT_EVERY_MODULE], cwd=work, env=env).stdout.strip()
        refused = cli("selftest", check=False)
        if refused.returncode != 2 or "installed package" not in refused.stderr:
            raise DistError(f"python {label}: selftest without the tests exited {refused.returncode}: "
                            f"{refused.stderr.strip()}")
        # The wheel alone has no extras: a command that needs one says which, without a traceback.
        lacking = cli("smoke", check=False)
        if lacking.returncode != 1 or not lacking.stderr.startswith("error: this command needs the ios extra") \
                or "Traceback" in lacking.stderr:
            raise DistError(f"python {label}: smoke without the extras exited {lacking.returncode}: "
                            f"{lacking.stderr.strip()}")
        answers = json.loads(run([vpython, "-I", "-c", TEMPLATE_ANSWERS], cwd=work, env=env).stdout)
        if sorted(answers["templates"]) != sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir()):
            raise DistError(f"python {label}: the package's templates are not examples/templates/'s")
        for name, args in sorted(answers["templates"].items()):
            app = base / f"app-{name}"
            app.mkdir()
            result = cli("init", *args, "--sha", answers["sha"], "--allow-untagged", "--no-input", cwd=app)
            if "Check the files: store-release config check --files" not in result.stdout:
                raise DistError(f"python {label}: init's next steps do not name the installed command:\n"
                                f"{result.stdout}")
            written, wanted = written_files(app), template_files(name)
            if written != wanted:
                differ = sorted(path for path in set(written) | set(wanted) if written.get(path) != wanted.get(path))
                raise DistError(f"python {label}: init ({name}) wrote files that differ from "
                                f"examples/templates/{name}/: {differ}")
            checked = cli("config", "check", cwd=app).stdout
            if "store/store-release.toml: schema 1, Example App" not in checked:
                raise DistError(f"python {label}: config check on init's {name} app printed {checked!r}")
        print(f"python {label}: the wheel alone runs --version, --help and python -m, imports its {modules} "
              f"modules, refuses selftest, names the extra smoke lacks, and init writes the "
              f"{len(answers['templates'])} templates")


def check(dist_dir: Path, pythons: list[str], *, root: Path = ROOT) -> None:
    version = package_version(root)
    dists = the_dists(dist_dir, version)
    result = run([sys.executable, "-I", "-m", "twine", "check", "--strict", *dists.values()])
    print(result.stdout.strip())
    check_contents(dists, version, root)
    for python in pythons or [sys.executable]:
        smoke(python, dists[dist_names(version)[1]], version)


# --- the GitHub Release --------------------------------------------------------------


class GhError(DistError):
    """A gh command failed."""


def run_gh(args: list[str]) -> str:
    """`gh <args>` stdout (GH_TOKEN stays in the environment, never in argv)."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def release_problem(release: object, tagged: str, files: dict[str, str], *, tag: str, sha: str,
                    complete: bool) -> str | None:
    """Why the release read is not tag, published, immutable, on sha and carrying files; None when it is."""
    if not isinstance(release, dict):
        return f"gh release view {tag} returned {type(release).__name__}, not an object"
    if release.get("isDraft") is not False or release.get("isImmutable") is not True:
        return (f"{tag} is not a published, immutable release (isDraft={release.get('isDraft')}, "
                f"isImmutable={release.get('isImmutable')})")
    if tagged != sha:
        return f"{tag} points at {tagged or 'nothing'}, not {sha}"
    assets = {asset.get("name"): asset.get("digest") for asset in release.get("assets") or []
              if isinstance(asset, dict)}
    differ = sorted(name for name, digest in files.items() if assets.get(name) != digest)
    if differ:
        return (f"{tag}'s {', '.join(differ)} {'is' if len(differ) == 1 else 'are'} not this run's file "
                "(missing, or another SHA-256)")
    if complete and set(assets) != set(files):
        return f"{tag} carries other assets than this run's: {sorted(set(assets) - set(files))}"
    return None


def release_check(dist_dir: Path, version: str, *, sha: str, repository: str, complete: bool = False,
                  gh: Gh = run_gh, attempts: int = GH_ATTEMPTS, interval: float = GH_INTERVAL,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    dist_names(version)
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise DistError(f"--sha {sha!r} is not a full commit SHA")
    if not REPOSITORY.fullmatch(repository or ""):
        raise DistError(f"--repository {repository!r} is not owner/name")
    if not dist_dir.is_dir():
        raise DistError(f"{dist_dir} is not a folder")
    files = {p.name: f"sha256:{sha256(p)}" for p in sorted(dist_dir.iterdir()) if p.is_file()}
    if not files:
        raise DistError(f"{dist_dir} has no files to compare")
    tag = f"v{version}"
    # Read and checked again for a minute: right after `gh release create`, GitHub may not show
    # the release, its tag or its assets as complete yet, and a false failure here would leave
    # the version off PyPI for good (the release job cannot run again for an existing tag).
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            release = json.loads(gh(["release", "view", tag, "--repo", repository, "--json", "isDraft,isImmutable,assets"]))
            tagged = gh(["api", f"repos/{repository}/git/ref/tags/{tag}", "--jq", ".object.sha"]).strip()
        except (GhError, ValueError) as exc:
            problem = f"cannot read the GitHub Release {tag} ({exc})"
        else:
            problem = release_problem(release, tagged, files, tag=tag, sha=sha, complete=complete)
            if problem is None:
                break
        if attempt >= attempts:
            raise DistError(problem)
        print(f"{problem}; trying again")
        sleep(interval)
    print(f"{tag} is published and immutable on {sha}, with {'exactly ' if complete else ''}these "
          f"{len(files)} files: {', '.join(files)}")


# --- PyPI ---------------------------------------------------------------------------


def fetch_json(url: str, *, opener: Opener, accept: str = "application/json") -> dict | None:
    """The JSON document at url, or None on 404; any other failure raises DistError."""
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{PROJECT}-release"})
    try:
        with opener(request, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise DistError(f"{url}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DistError(f"{url}: {exc}") from None
    if not isinstance(data, dict):
        raise DistError(f"{url}: unexpected response")
    return data


def pypi_files(version: str, *, opener: Opener, base: str = PYPI) -> dict[str, str]:
    """{file name: sha256} of the files PyPI lists for the version ({} when it has no such release)."""
    data = fetch_json(f"{base}/pypi/{PROJECT}/{urllib.parse.quote(version)}/json", opener=opener)
    files: dict[str, str] = {}
    for entry in (data or {}).get("urls") or []:
        name = entry.get("filename") if isinstance(entry, dict) else None
        digest = (entry.get("digests") or {}).get("sha256") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(digest, str):
            raise DistError(f"PyPI's {PROJECT} {version}: unexpected file entry {entry!r}")
        files[name] = digest
    return files


def compare(local: dict[str, str], remote: dict[str, str], version: str) -> list[str]:
    """The local files PyPI does not have yet; fails on a different digest or a file the release did not build."""
    foreign = sorted(set(remote) - set(local))
    if foreign:
        raise PypiMismatch(f"PyPI has {', '.join(foreign)} for {PROJECT} {version}, which this release did not "
                           "build")
    differ = sorted(name for name in local if name in remote and remote[name] != local[name])
    if differ:
        raise PypiMismatch(f"PyPI has {', '.join(differ)} with another SHA-256 than this release's file: a file "
                           "name is never reused, so release the next patch (docs/release-process.md)")
    return [name for name in local if name not in remote]


def pypi_plan(dist_dir: Path, version: str, upload_dir: Path, *, opener: Opener = urllib.request.urlopen,
              base: str = PYPI, environ: dict[str, str] | None = None) -> list[str]:
    local = {name: sha256(path) for name, path in the_dists(dist_dir, version).items()}
    missing = compare(local, pypi_files(version, opener=opener, base=base), version)
    empty_dir(upload_dir, "--upload-dir")
    for name in local:
        if name in missing:
            shutil.copyfile(dist_dir / name, upload_dir / name)
            print(f"to upload: {name}  sha256 {local[name]}")
        else:
            print(f"already on PyPI with this SHA-256: {name}")
    env = os.environ if environ is None else environ
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"upload={'true' if missing else 'false'}\n")
    return missing


def provenance_publishers(version: str, name: str, *, opener: Opener, base: str = PYPI) -> list[dict]:
    """The publishers of the file's PEP 740 attestation bundles ([] when PyPI has none yet)."""
    url = f"{base}/integrity/{PROJECT}/{urllib.parse.quote(version)}/{urllib.parse.quote(name)}/provenance"
    data = fetch_json(url, opener=opener, accept="application/vnd.pypi.integrity.v1+json")
    bundles = (data or {}).get("attestation_bundles") or []
    return [bundle["publisher"] for bundle in bundles
            if isinstance(bundle, dict) and bundle.get("attestations") and isinstance(bundle.get("publisher"), dict)]


def published_by(publishers: list[dict], expected: dict[str, str]) -> bool:
    """True when one publisher is expected's (GitHub names compare without case; other keys are ignored)."""
    return any(all(str(publisher.get(key) or "").lower() == value.lower() for key, value in expected.items())
               for publisher in publishers)


def pypi_verify(dist_dir: Path, version: str, *, repository: str, workflow: str = "release.yml",
                environment: str = "pypi", opener: Opener = urllib.request.urlopen, base: str = PYPI,
                timeout: float = VERIFY_TIMEOUT, interval: float = VERIFY_INTERVAL,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
    if not REPOSITORY.fullmatch(repository or ""):
        raise DistError(f"--repository {repository!r} is not owner/name")
    local = {name: sha256(path) for name, path in the_dists(dist_dir, version).items()}
    publisher = {"kind": "GitHub", "repository": repository, "workflow": workflow}
    deadline = clock() + timeout
    while True:
        publishers: dict[str, list[dict]] = {}
        try:
            waiting = compare(local, pypi_files(version, opener=opener, base=base), version)
            if not waiting:
                publishers = {name: provenance_publishers(version, name, opener=opener, base=base) for name in local}
                waiting = [f"{name}'s attestation by {repository} {workflow}" for name in local
                           if not published_by(publishers[name], publisher)]
        except PypiMismatch:
            raise
        except DistError as exc:
            # PyPI unreachable or answering an error: wait like for a file it does not list yet.
            waiting = [f"a readable answer ({exc})"]
        if not waiting:
            for name, digest in local.items():
                print(f"PyPI serves {name}  sha256 {digest}, attested by {repository} {workflow}")
                if not published_by(publishers[name], {**publisher, "environment": environment}):
                    # Published and attested; only the trusted publisher's settings are looser than documented.
                    print(f"::warning::PyPI's trusted publisher for {PROJECT} does not require the {environment!r} "
                          f"environment ({name}); set it on PyPI (docs/release-process.md)")
            return
        if clock() >= deadline:
            raise DistError(f"after {timeout:.0f}s PyPI still lacks {', '.join(waiting)} for {PROJECT} {version}")
        print(f"waiting for {', '.join(waiting)}")
        sleep(interval)


# --- command line -------------------------------------------------------------------


def main(argv: list[str] | None = None, *, opener: Opener = urllib.request.urlopen, gh: Gh = run_gh) -> int:
    parser = argparse.ArgumentParser(prog="dists.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build_p = commands.add_parser("build")
    build_p.add_argument("--out", type=Path, required=True)
    build_p.add_argument("--version")
    check_p = commands.add_parser("check")
    check_p.add_argument("dist_dir", type=Path)
    check_p.add_argument("--python", action="append", default=[])
    release_p = commands.add_parser("release-check")
    release_p.add_argument("dist_dir", type=Path)
    release_p.add_argument("--version", required=True)
    release_p.add_argument("--sha", required=True)
    release_p.add_argument("--repository", required=True)
    release_p.add_argument("--complete", action="store_true")
    plan_p = commands.add_parser("pypi-plan")
    plan_p.add_argument("dist_dir", type=Path)
    plan_p.add_argument("--version", required=True)
    plan_p.add_argument("--upload-dir", type=Path, required=True)
    verify_p = commands.add_parser("pypi-verify")
    verify_p.add_argument("dist_dir", type=Path)
    verify_p.add_argument("--version", required=True)
    verify_p.add_argument("--repository", required=True)
    verify_p.add_argument("--workflow", default="release.yml")
    verify_p.add_argument("--environment", default="pypi")
    verify_p.add_argument("--timeout", type=float, default=VERIFY_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build(args.out, version=args.version)
        elif args.command == "check":
            check(args.dist_dir, args.python)
        elif args.command == "release-check":
            release_check(args.dist_dir, args.version, sha=args.sha, repository=args.repository,
                          complete=args.complete, gh=gh)
        elif args.command == "pypi-plan":
            pypi_plan(args.dist_dir, args.version, args.upload_dir, opener=opener)
        else:
            pypi_verify(args.dist_dir, args.version, repository=args.repository, workflow=args.workflow,
                        environment=args.environment, opener=opener, timeout=args.timeout)
    except (DistError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
