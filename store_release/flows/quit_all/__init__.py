"""Quit All's release flow (apoorvdarshan/quit-all scripts/store, ported unchanged): see store_release.flows."""
from __future__ import annotations

from pathlib import Path

from store_release.flows import check_catalog_groups, require
from store_release.flows.rebind import rebind

GROUPS = ("subscriptions", "non_consumables", "consumables", "tips")
MODULES = ("asc_release", "ios_release")


def configure(config, root: Path) -> None:
    from store_release.flows.quit_all import asc_release, ios_release  # noqa: PLC0415

    ios = config.ios
    check_catalog_groups(config, "quit-all", GROUPS)
    require(bool(config.catalog.storekit and config.catalog.products_source),
            "catalog: the quit-all flow checks the catalog against catalog.storekit and catalog.products_source "
            "(the PaywallManager.swift that lists the products and entitlementIDs)")
    listing_dir = ios.listing.dir if ios.listing else "store/metadata/ios"
    require(listing_dir.endswith("/ios"), "ios.listing.dir: the quit-all flow reads <metadata>/ios/<locale>/")
    version_file = (ios.whats_new.version_file if ios.whats_new else None) or "whats_new_version.txt"
    rebind(asc_release, {
        "ROOT": root,
        "DEFAULT_BUNDLE_ID": ios.bundle_id,
        "DEFAULT_APP_ID": ios.asc_app_id,
        "METADATA_DIR": root / listing_dir.rsplit("/", 1)[0],
        "CATALOG": root / config.catalog.path,
        "DEFAULT_CREATIVE_DIR": root / (ios.creative_dir or "store/creative/ios"),
        "CREATIVE_LOCALE": ios.locale,
        "SCREENSHOT_MANIFEST": ios.screenshot_manifest or "screenshots.json",
        "WHATS_NEW_VERSION_FILE": version_file,
    })
    rebind(ios_release, {
        "ROOT": root,
        "PBXPROJ": root / ios.pbxproj,
        "BUNDLE_ID": ios.bundle_id,
        "ASC_APP_ID": ios.asc_app_id,
        "TAG_PREFIX": ios.tag_prefix,
    })
