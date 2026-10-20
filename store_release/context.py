"""Point every ported module at one app: configure(config, app_root) sets their app constants.

The ported modules keep their functions and messages as they were in the
upstream scripts; only their app constants (paths, bundle ids, tag prefixes,
locales, sets) became module globals, set here once per process. A module
used before configure() fails with RuntimeError (store_release.UNCONFIGURED).
"""
from __future__ import annotations

from pathlib import Path

from store_release import catalog, release_notes, screenshots
from store_release.android import play_listing
from store_release.android import release as android_release
from store_release.config import Config
from store_release.ios import asc
from store_release.ios import release as ios_release
from store_release.text import listing, whats_new


def tables(config: Config) -> frozenset[str]:
    """The optional per-switch tables this config has (ios.listing, android.screenshots, ...)."""
    present: set[str] = set()
    if config.ios is not None:
        ios = config.ios
        for name, value in (("ios.listing", ios.listing), ("ios.whats_new", ios.whats_new),
                            ("ios.screenshots", ios.screenshot_sets), ("ios.creative", ios.creative_dir),
                            ("ios.products", ios.iap_route)):
            if value is not None:
                present.add(name)
    if config.android is not None:
        android = config.android
        for name, value in (("android.listing", android.listing), ("android.whats_new", android.whats_new),
                            ("android.screenshots", android.screenshots_dir)):
            if value is not None:
                present.add(name)
    return frozenset(present)


def configure(config: Config, app_root: Path, *, config_path: str = "store/store-release.toml") -> None:
    root = Path(app_root)
    present = tables(config)

    release_notes.ROOT = root
    release_notes.NOTES_RELPATH = config.release_notes

    catalog.ROOT = root
    if config.catalog is not None:
        catalog.PRODUCTS = root / config.catalog.path
        catalog.REVENUECAT = root / config.catalog.revenuecat if config.catalog.revenuecat else None
        catalog.GROUPS = config.catalog.groups
        catalog.ID_PREFIX = config.catalog.id_prefix
        catalog.PLAY_RULES = config.catalog.play_rules
        catalog.CHECKS = config.catalog.checks
    else:
        catalog.PRODUCTS = None
        catalog.REVENUECAT = None
        catalog.GROUPS = ()
        catalog.ID_PREFIX = ""
        catalog.PLAY_RULES = False
        catalog.CHECKS = ()

    ios = config.ios
    android = config.android

    listing.ROOT = root
    whats_new.ROOT = root
    screenshots.ROOT = root
    screenshots.STAGING = None
    asc.ROOT = root
    asc.APP_NAME = config.app.name
    asc.CATALOG_PATH = root / config.catalog.path if config.catalog is not None else None
    asc.CATALOG_GROUPS = config.catalog.groups if config.catalog is not None else ()
    staged = root / "store" / "metadata" / "screenshots"

    if ios is not None:
        ios_release.ROOT = root
        ios_release.PBXPROJ = root / ios.pbxproj
        ios_release.APP_TARGET = ios.app_target
        ios_release.BUNDLE_ID = ios.bundle_id
        ios_release.TAG_PREFIX = ios.tag_prefix
        ios_release.LOCALE = ios.locale
        ios_release.CONFIG_PATH = config_path
        ios_release.TABLES = present

        sets = [s for s in ios.screenshot_sets or () if s.display_type != "WATCH"]
        watch = [s for s in ios.screenshot_sets or () if s.display_type == "WATCH"]
        asc.DEFAULT_BUNDLE_ID = ios.bundle_id
        asc.ASC_APP_ID = ios.asc_app_id
        asc.SCREENSHOT_SETS = tuple(asc.SCREENSHOT_SET_TABLE[s.display_type] for s in sets)
        asc.DEFAULT_CREATIVE_DIR = root / (ios.creative_dir or "store/creative/ios")
        asc.LOCALE = ios.locale

        screenshots.ASC_SETS = tuple(
            screenshots.AscSet(
                asc.SCREENSHOT_SET_TABLE[s.display_type][2],
                root / s.dir,
                staged / "ios" / asc.SCREENSHOT_SET_TABLE[s.display_type][0],
                screenshots.ACCEPTED_SIZES[s.display_type],
            )
            for s in sets
        )
        screenshots.WATCH_DIR = watch[0].dir if watch else None

        listing.IOS_MARKDOWN = ios.listing.markdown if ios.listing else "APPSTORE.md"
        listing.IOS_LOCALE = ios.locale
        whats_new.IOS_MARKDOWN = ios.whats_new.markdown if ios.whats_new else listing.IOS_MARKDOWN
        whats_new.IOS_HEADING = ios.whats_new.heading if ios.whats_new else "What's New"
        whats_new.IOS_LOCALE = ios.locale
    else:
        screenshots.ASC_SETS = ()
        screenshots.WATCH_DIR = None

    if android is not None:
        android_release.ROOT = root
        android_release.GRADLE = root / android.gradle_file
        android_release.GRADLE_RELPATH = android.gradle_file
        android_release.PACKAGE_NAME = android.package
        android_release.TAG_PREFIX = android.tag_prefix
        android_release.UPLOAD_CERT_SHA256 = android.upload_cert_sha256
        android_release.CONFIG_PATH = config_path
        android_release.TABLES = present

        play_listing.ROOT = root
        play_listing.PACKAGE_NAME = android.package
        play_listing.LOCALE = android.locale

        listing.PLAY_MARKDOWN = android.listing.markdown if android.listing else "PLAYSTORE.md"
        listing.PLAY_LOCALE = android.locale
        whats_new.PLAY_MARKDOWN = android.whats_new.markdown if android.whats_new else "PLAYSTORE.md"
        whats_new.PLAY_HEADING = android.whats_new.heading if android.whats_new else "What's New"

        screenshots.PLAY_SOURCE = root / android.screenshots_dir if android.screenshots_dir else None
        screenshots.PLAY_PHONE_DIR = staged / "play" / "phone"
    else:
        screenshots.PLAY_SOURCE = None
        screenshots.PLAY_PHONE_DIR = staged / "play" / "phone"
