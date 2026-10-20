"""Create or update the tag's GitHub Release with the gh CLI (argv lists, never a shell).

iOS:     title from ios.github_release.title; reviewed notes or generated ones; with
         ios.github_release.source_archive, the signed source archive's files. An existing
         release is handled as ios.github_release.on_existing says (update, upload-missing,
         keep or fail).
Android: title "Android {version} (versionCode {version_code})" (github-preflight reads
         it), the signed AAB and APK with their Sigstore bundles; a pre-release
         unless the build went to Play production.

A release is marked Latest only when its tag is the highest version among the
non-draft releases with the same tag prefix, so re-running an older tag's
release step never takes Latest from a newer release.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from store_release.config import Config, render_template

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class GithubError(Exception):
    """A GitHub Release precondition or gh call failed."""


def run_gh(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def version_key(version: str) -> tuple[int, ...] | None:
    from store_release.ios.asc import version_key as key  # noqa: PLC0415 - the one version ordering

    return key(version)


def is_highest(tag: str, prefix: str, tags: Sequence[str]) -> bool:
    """True when every other tag with this prefix (whose version parses) sorts strictly below tag."""
    mine = version_key(tag[len(prefix):]) if tag.startswith(prefix) else None
    if mine is None:
        return False
    for other in tags:
        if other == tag or not other.startswith(prefix):
            continue
        key = version_key(other[len(prefix):])
        if key is not None and key >= mine:
            return False
    return True


def release_tags(repo: str, run: Runner = run_gh) -> list[str]:
    result = run(["gh", "release", "list", "--repo", repo, "--limit", "1000", "--exclude-drafts",
                  "--json", "tagName"])
    if result.returncode != 0:
        raise GithubError(f"gh release list failed: {(result.stderr or result.stdout).strip()}")
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GithubError(f"gh release list returned invalid JSON: {exc}") from None
    return [row.get("tagName") or "" for row in rows]


def notes_args(notes_file: str) -> list[str]:
    return ["--notes-file", notes_file] if notes_file and Path(notes_file).is_file() and Path(notes_file).stat().st_size else []


def release_exists(tag: str, repo: str, run: Runner) -> bool:
    return run(["gh", "release", "view", tag, "--repo", repo]).returncode == 0


def ios_title(config: Config, app_root: Path, *, version: str, uploaded_build: str, tag: str = "") -> str:
    build = uploaded_build
    if not build and "{build}" in config.ios.title:
        # The first line naming CURRENT_PROJECT_VERSION, digits only (`grep -m1 | tr -dc 0-9`).
        pbxproj = (app_root / config.ios.pbxproj).read_text(encoding="utf-8")
        line = next((line for line in pbxproj.splitlines() if "CURRENT_PROJECT_VERSION" in line), "")
        build = "".join(ch for ch in line if ch.isdigit())
    # " (build N)" only when this run uploaded a build.
    suffix = f" (build {uploaded_build})" if uploaded_build else ""
    return render_template(config.ios.title, version=version, build=build, tag=tag, build_suffix=suffix)


SOURCE_ARCHIVE_SUFFIXES = ("", ".sigstore.json", ".sha256")


def source_archive_name(config: Config, version: str) -> str:
    return f"{config.app.slug}-{version}-source.tar.gz"


def source_archive_assets(config: Config, version: str, assets_dir: str) -> list[str]:
    """The signed source archive, its Sigstore bundle and its checksum, in the order they are attached."""
    if not config.ios.source_archive:
        return []
    if not assets_dir:
        raise GithubError("ios.github_release.source_archive is on, but no --assets-dir holds the archive")
    base = Path(assets_dir) / source_archive_name(config, version)
    assets = [str(base) + suffix for suffix in SOURCE_ARCHIVE_SUFFIXES]
    missing = [Path(a).name for a in assets if not Path(a).is_file()]
    if missing:
        raise GithubError(f"the source archive files are missing from {assets_dir}: {', '.join(missing)}")
    return assets


def attached_assets(tag: str, repo: str, run: Runner) -> list[str]:
    result = run(["gh", "release", "view", tag, "--repo", repo, "--json", "assets", "--jq", ".assets[].name"])
    if result.returncode != 0:
        raise GithubError(f"gh release view failed: {(result.stderr or result.stdout).strip()}")
    return [line for line in (result.stdout or "").splitlines() if line]


def ios_release_commands(config: Config, app_root: Path, *, tag: str, version: str, uploaded_build: str,
                         notes_file: str, repo: str, tags: Sequence[str], exists: bool, assets: Sequence[str] = (),
                         attached: Sequence[str] | None = None) -> list[list[str]]:
    title = ios_title(config, app_root, version=version, uploaded_build=uploaded_build, tag=tag)
    notes = notes_args(notes_file)
    if config.ios.latest == "always":
        latest = ["--latest"]
    else:
        latest = ["--latest"] if is_highest(tag, config.ios.tag_prefix, tags) else ["--latest=false"]
    policy = config.ios.on_existing
    if exists and policy == "update":
        return [["gh", "release", "edit", tag, "--repo", repo, "--title", title, *notes,
                 "--draft=false", "--prerelease=false", *latest]]
    if exists and policy == "keep":
        print(f"GitHub Release {tag} already exists; leaving it unchanged")
        return []
    if exists and policy == "upload-missing":
        print(f"GitHub Release {tag} already exists; leaving its notes unchanged")
        names = set(attached or ())
        if assets and not all(Path(asset).name in names for asset in assets):
            # The archive, its signature and its checksum are attached as one set.
            return [["gh", "release", "upload", tag, "--repo", repo, *assets, "--clobber"]]
        if assets:
            print("The signed source archive is already attached")
        return []
    # A new release, or on_existing = "fail": gh refuses to create a release that exists.
    return [["gh", "release", "create", tag, "--repo", repo, "--verify-tag", "--title", title,
             *(notes or ["--generate-notes"]), *latest, *assets]]


def android_version_code(config: Config, app_root: Path) -> int:
    from store_release.android import release as android_release  # noqa: PLC0415

    _, code = android_release.gradle_version((app_root / config.android.gradle_file).read_text(encoding="utf-8"))
    return code


def android_asset_base(config: Config, app_root: Path, tag: str) -> tuple[str, str, int]:
    prefix = config.android.tag_prefix
    if not tag.startswith(prefix):
        raise GithubError(f"{tag} is not an {prefix}* tag")
    version = tag[len(prefix):]
    code = android_version_code(config, app_root)
    base = render_template(config.android.asset_name, slug=config.app.slug, version=version, version_code=str(code))
    return base, version, code


def android_release_commands(config: Config, app_root: Path, *, tag: str, production: bool, assets_dir: Path,
                             notes_file: str, repo: str, tags: Sequence[str], exists: bool) -> list[list[str]]:
    base, version, code = android_asset_base(config, app_root, tag)
    title = f"Android {version} (versionCode {code})"
    assets = [str(assets_dir / f"{base}{suffix}")
              for suffix in (".aab", ".aab.sigstore.json", ".apk", ".apk.sigstore.json")]
    highest = is_highest(tag, config.android.tag_prefix, tags)
    if production and (highest or config.android.flow == "verceltics"):
        # The verceltics flow marked every production release Latest.
        kind = ["--prerelease=false", "--latest"]
    elif production:
        kind = ["--prerelease=false", "--latest=false"]
    else:
        # A pre-release while Android ships only to testing tracks, so the iOS release stays Latest.
        kind = ["--prerelease", "--latest=false"]
    notes = notes_args(notes_file)
    if exists and config.android.flow == "verceltics":
        # The verceltics flow edits the release first, then replaces its assets.
        return [
            ["gh", "release", "edit", tag, "--repo", repo, "--verify-tag", *kind, "--title", title, *notes],
            ["gh", "release", "upload", tag, "--repo", repo, "--clobber", *assets],
        ]
    if exists:
        return [
            ["gh", "release", "upload", tag, "--repo", repo, "--clobber", *assets],
            ["gh", "release", "edit", tag, "--repo", repo, "--title", title, *notes, *kind, "--draft=false"],
        ]
    return [["gh", "release", "create", tag, "--repo", repo, "--verify-tag", "--title", title,
             *(notes or ["--generate-notes"]), *kind, *assets]]


def stage_android_assets(config: Config, app_root: Path, *, tag: str, out: Path) -> list[Path]:
    base, _, _ = android_asset_base(config, app_root, tag)
    out.mkdir(parents=True, exist_ok=True)
    copied = []
    for source, suffix in ((config.android.build.aab, ".aab"), (config.android.build.apk, ".apk")):
        target = out / f"{base}{suffix}"
        shutil.copyfile(app_root / source, target)
        copied.append(target)
        print(f"staged {source} -> {target}")
    return copied


def execute(commands: list[list[str]], run: Runner = run_gh) -> None:
    for argv in commands:
        print("$ " + " ".join(argv[:4]) + " ...", flush=True)
        result = run(argv)
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            raise GithubError(f"{' '.join(argv[:3])} failed ({result.returncode}): {(result.stderr or '').strip()}")


def release(config: Config, app_root: Path, *, platform: str, tag: str, notes_file: str, version: str = "",
            uploaded_build: str = "", production: str = "", assets_dir: str = "", run: Runner = run_gh,
            environ: dict[str, str] | None = None) -> list[list[str]]:
    env = os.environ if environ is None else environ
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise GithubError("GITHUB_REPOSITORY is not set")
    tags = release_tags(repo, run)
    exists = release_exists(tag, repo, run)
    if exists and (platform == "android" or config.ios is None or config.ios.on_existing == "update"):
        print(f"the GitHub Release for {tag} exists (earlier attempt); updating it")
    if platform == "ios":
        if config.ios is None:
            raise GithubError("no [ios] in store-release.toml")
        assets = source_archive_assets(config, version, assets_dir)
        attached = attached_assets(tag, repo, run) if exists and config.ios.on_existing == "upload-missing" else None
        commands = ios_release_commands(config, app_root, tag=tag, version=version, uploaded_build=uploaded_build,
                                        notes_file=notes_file, repo=repo, tags=tags, exists=exists, assets=assets,
                                        attached=attached)
    else:
        if config.android is None:
            raise GithubError("no [android] in store-release.toml")
        if production not in ("true", "false"):
            raise GithubError(f"--production {production!r} must be true or false")
        commands = android_release_commands(config, app_root, tag=tag, production=production == "true",
                                            assets_dir=Path(assets_dir), notes_file=notes_file, repo=repo,
                                            tags=tags, exists=exists)
    execute(commands, run)
    return commands


def main_error(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1
