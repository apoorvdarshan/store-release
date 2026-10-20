"""ABYSM's release flow (apoorvdarshan/abysm scripts/store, ported unchanged): see store_release.flows."""
from __future__ import annotations

from pathlib import Path

from store_release.flows import check_catalog_groups, require, set_dirs
from store_release.flows.rebind import rebind

GROUPS = ("nonConsumables", "consumables", "subscriptions")
MODULES = ("asc_release", "ios_release", "export_listing", "validate_catalog")


def configure(config, root: Path) -> None:
    from store_release.flows.abysm import asc_release, ios_release, validate_catalog  # noqa: PLC0415

    ios = config.ios
    check_catalog_groups(config, "abysm", GROUPS)
    catalog = config.catalog
    require(bool(catalog.storekit and catalog.products_source),
            "catalog: the abysm flow checks the catalog against catalog.storekit and catalog.products_source "
            "(the StoreManager.swift that names productID and entitlementID)")
    listing_dir = ios.listing.dir if ios.listing else "store/metadata/ios"
    require(listing_dir.endswith("/ios"), "ios.listing.dir: the abysm flow reads <metadata>/ios/<locale>/")
    rebind(asc_release, {"ROOT": root, "DEFAULT_BUNDLE_ID": ios.bundle_id, "APP_STORE_APP_ID": ios.asc_app_id,
                         "METADATA_DIR": root / listing_dir.rsplit("/", 1)[0], "CATALOG": root / catalog.path,
                         "PRIMARY_LOCALE": ios.locale,
                         "DEFAULT_CREATIVE_DIR": root / (ios.creative_dir or "store/creative/ios")})
    dirs = set_dirs(config, "abysm")
    if dirs:
        sets = tuple((d, root / dirs[d], sizes) for d, _, sizes in asc_release.SCREENSHOT_SETS)
        optional = tuple((d, root / dirs[d], sizes) for d, _, sizes in asc_release.OPTIONAL_SCREENSHOT_SETS)
        rebind(asc_release, {"SCREENSHOT_SETS": sets, "OPTIONAL_SCREENSHOT_SETS": optional,
                             "WATCH_SCREENSHOTS": root / dirs["WATCH"]})
    rebind(ios_release, {"ROOT": root, "PBXPROJ": root / ios.pbxproj, "BUNDLE_ID": ios.bundle_id,
                         "APP_STORE_APP_ID": ios.asc_app_id, "TAG_PREFIX": ios.tag_prefix})
    rebind(validate_catalog, {"ROOT": root, "CATALOG": root / catalog.path, "STOREKIT": root / catalog.storekit,
                              "STORE_MANAGER": root / catalog.products_source, "BUNDLE_ID": ios.bundle_id})
