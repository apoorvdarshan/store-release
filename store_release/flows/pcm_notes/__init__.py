"""PCM Notes' release flow (apoorvdarshan/pcm-notes scripts/store, ported unchanged): see store_release.flows."""
from __future__ import annotations

from pathlib import Path

from store_release.flows import check_catalog_groups, require, set_dirs
from store_release.flows.rebind import rebind

GROUPS = ("subscriptions", "consumables", "tips")
MODULES = ("asc_release", "ios_release", "export_listing", "validate_catalog")


def configure(config, root: Path) -> None:
    from store_release.flows.pcm_notes import asc_release, ios_release, validate_catalog  # noqa: PLC0415

    ios = config.ios
    check_catalog_groups(config, "pcm-notes", GROUPS)
    catalog = config.catalog
    require(bool(catalog.storekit and catalog.products_source),
            "catalog: the pcm-notes flow checks the catalog against catalog.storekit and catalog.products_source "
            "(the TipStore.swift that lists TipProductID)")
    listing_dir = ios.listing.dir if ios.listing else "store/metadata/ios"
    require(listing_dir.endswith("/ios"), "ios.listing.dir: the pcm-notes flow reads <metadata>/ios/<locale>/")
    rebind(asc_release, {"ROOT": root, "DEFAULT_BUNDLE_ID": ios.bundle_id, "APP_STORE_APP_ID": ios.asc_app_id,
                         "PRIMARY_LOCALE": ios.locale,
                         "DEFAULT_CREATIVE_DIR": root / (ios.creative_dir or "store/creative/ios")})
    dirs = set_dirs(config, "pcm-notes")
    if dirs:
        sets = tuple(s._replace(directory=dirs[s.display_type]) for s in asc_release.SCREENSHOT_SETS)
        optional = tuple(s._replace(directory=dirs[s.display_type]) for s in asc_release.OPTIONAL_SCREENSHOT_SETS)
        rebind(asc_release, {"SCREENSHOT_SETS": sets, "OPTIONAL_SCREENSHOT_SETS": optional,
                             "WATCH_FOLDER": dirs["WATCH"]})
    rebind(ios_release, {"ROOT": root, "PBXPROJ": root / ios.pbxproj,
                         "METADATA_DIR": root / listing_dir.rsplit("/", 1)[0],
                         "BUNDLE_ID": ios.bundle_id, "TAG_PREFIX": ios.tag_prefix})
    rebind(validate_catalog, {"ROOT": root, "PRODUCTS": root / catalog.path, "TIP_STORE": root / catalog.products_source,
                              "STOREKIT": root / catalog.storekit, "BUNDLE_ID": ios.bundle_id})
