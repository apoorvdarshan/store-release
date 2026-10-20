"""Per-job sparse-checkout patterns for the app checkout (`paths --job ID`).

The first line is always the config. Derived paths come next, in a fixed order
per job; the config's own `checkout` patterns come last, so their `!`
negations win. Duplicates are dropped, keeping the first occurrence. The app
checkout is non-cone and blobless, so files outside these patterns (such as a
large asset corpus) are never downloaded.
"""
from __future__ import annotations

import os
import re

from store_release.config import Config, ConfigError

JOBS = (
    "ios-prepare",
    "ios-build",
    "ios-wait-for-build",
    "ios-app-store-connect",
    "ios-github-release",
    "ios-source-archive",
    "ios-store-plan",
    "android-gates",
    "android-publish",
    "android-store-plan",
    "checks",
)

# What each job reads, in order. Parts a config does not have are skipped.
RECIPES = {
    "ios-prepare": ("caller", "store", "pbxproj", "info_plist", "release_notes", "ios_listing", "ios_whats_new",
                    "catalog_hooks", "catalog_sources", "prepare_hooks", "prepare_checkout"),
    "ios-build": ("pbxproj", "build_hooks", "ios_checkout"),
    "ios-wait-for-build": (),
    "ios-app-store-connect": ("store", "ios_listing", "ios_whats_new", "flow_release_notes", "catalog_sources"),
    "ios-github-release": ("release_notes", "pbxproj"),
    "ios-source-archive": (),
    "ios-store-plan": ("store", "ios_listing", "ios_whats_new", "pbxproj", "flow_release_notes", "catalog_sources"),
    "android-gates": ("caller", "gradle", "release_notes", "play_listing", "play_whats_new"),
    "android-publish": ("store", "release_notes", "play_listing", "play_whats_new", "catalog_hooks",
                        "catalog_sources", "android_checkout"),
    "android-store-plan": ("gradle", "store", "play_listing", "play_whats_new", "release_notes"),
    # config check --files verifies every hook script exists, so the build hooks are checked out too.
    "checks": ("workflows", "store", "ios_listing", "ios_whats_new", "play_listing", "play_whats_new",
               "release_notes", "pbxproj", "info_plist", "gradle", "catalog_hooks", "build_hooks",
               "catalog_sources", "prepare_hooks"),
}

CALLER_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")


def caller_path(workflow_ref: str, repository: str) -> str:
    """The caller workflow's repository path from GITHUB_WORKFLOW_REF (owner/repo/<path>@<ref>)."""
    path = workflow_ref.split("@", 1)[0]
    prefix = f"{repository}/"
    if not repository or not path.startswith(prefix):
        raise ConfigError(f"GITHUB_WORKFLOW_REF {workflow_ref!r} is not a workflow of {repository or 'this repository'}")
    path = path[len(prefix):]
    if not CALLER_PATH.fullmatch(path):
        raise ConfigError(f"GITHUB_WORKFLOW_REF names {path!r}, not a .github/workflows file")
    return path


def _store_paths(config: Config) -> list[str]:
    """/store/, plus any catalog, screenshot or creative path the config keeps outside it."""
    found = ["/store/"]
    outside: list[str] = []
    if config.catalog is not None:
        outside.append(config.catalog.path)
        if config.catalog.revenuecat:
            outside.append(config.catalog.revenuecat)
    if config.ios is not None:
        outside.extend(f"{s.dir}/" for s in config.ios.screenshot_sets or ())
        if config.ios.creative_dir:
            outside.append(f"{config.ios.creative_dir}/")
    if config.android is not None and config.android.screenshots_dir:
        outside.append(f"{config.android.screenshots_dir}/")
    found.extend(f"/{path}" for path in outside if not path.startswith("store/"))
    return found


def _part(config: Config, part: str, environ: dict[str, str]) -> list[str]:
    ios = config.ios
    android = config.android
    if part == "caller":
        return ["/" + caller_path(environ.get("GITHUB_WORKFLOW_REF", ""), environ.get("GITHUB_REPOSITORY", ""))]
    if part == "workflows":
        return ["/.github/workflows/"]
    if part == "store":
        return _store_paths(config)
    if part == "release_notes":
        return [f"/{config.release_notes}"]
    if part == "catalog_hooks":
        return [f"/{hook}" for hook in config.catalog.checks] if config.catalog else []
    if part == "catalog_sources":
        # The app files a ported flow's catalog check reads (StoreKit file, product constants).
        catalog = config.catalog
        return [f"/{path}" for path in (catalog.storekit, catalog.products_source) if path] if catalog else []
    if part == "prepare_hooks":
        return [f"/{hook}" for hook in ios.prepare_checks] if ios else []
    if part == "prepare_checkout":
        return list(ios.prepare_checkout) if ios else []
    if part == "flow_release_notes":
        # What's New from the tag's release notes (the verceltics flow) reads RELEASE_NOTES.md.
        if ios and ios.whats_new and ios.whats_new.source == "release-notes":
            return [f"/{config.release_notes}"]
        return []
    if part == "pbxproj":
        return [f"/{ios.pbxproj}"] if ios else []
    if part == "info_plist":
        return [f"/{ios.info_plist}"] if ios and ios.info_plist else []
    if part == "ios_listing":
        if ios and ios.listing and ios.listing.source == "files":
            return [] if ios.listing.dir.startswith("store/") else [f"/{ios.listing.dir}/"]
        return [f"/{ios.listing.markdown}"] if ios and ios.listing else []
    if part == "ios_whats_new":
        if ios and ios.whats_new and ios.whats_new.source != "markdown":
            return []
        return [f"/{ios.whats_new.markdown}"] if ios and ios.whats_new else []
    if part == "build_hooks":
        return [f"/{hook}" for hook in (*ios.build.pre_archive, *ios.build.post_archive)] if ios else []
    if part == "ios_checkout":
        return list(ios.build.checkout) if ios else []
    if part == "gradle":
        return [f"/{android.gradle_file}"] if android else []
    if part == "play_listing":
        if android and android.listing and android.listing.source == "files":
            return [] if android.listing.dir.startswith("store/") else [f"/{android.listing.dir}/"]
        return [f"/{android.listing.markdown}"] if android and android.listing else []
    if part == "play_whats_new":
        if android and android.whats_new and android.whats_new.source != "markdown":
            return []
        return [f"/{android.whats_new.markdown}"] if android and android.whats_new else []
    if part == "android_checkout":
        return list(android.build.checkout) if android else []
    raise AssertionError(part)


def patterns(config: Config, job: str, *, config_path: str, environ: dict[str, str] | None = None) -> list[str]:
    if job not in RECIPES:
        raise ConfigError(f"unknown job {job!r}; expected one of {', '.join(JOBS)}")
    if job.startswith("ios-") and config.ios is None:
        raise ConfigError(f"{job} needs an [ios] table in {config_path}")
    if job.startswith("android-") and config.android is None:
        raise ConfigError(f"{job} needs an [android] table in {config_path}")
    env = dict(os.environ if environ is None else environ)
    result: list[str] = []
    for line in [f"/{config_path}"] + [p for part in RECIPES[job] for p in _part(config, part, env)]:
        if line not in result:
            result.append(line)
    for line in result:
        if "\n" in line or "\r" in line or not line.strip():
            raise ConfigError(f"unsafe sparse pattern {line!r}")
    return result
