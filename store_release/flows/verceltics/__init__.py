"""Verceltics' release flows, iOS and Android (apoorvdarshan/verceltics scripts/store, ported unchanged):
see store_release.flows."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from store_release.flows import check_catalog_groups, require, set_dirs
from store_release.flows.rebind import rebind

GROUPS = ("subscriptions", "non_consumables", "tips")
# The staged screenshots keep their source folder's name (stage_screenshots), and the store
# step looks the Watch frames up under WATCH_FOLDER, so the folder names are part of the flow.
FOLDER_NAMES = {"APP_IPHONE_67": "iphone", "APP_IPAD_PRO_3GEN_129": "ipad", "APP_IPHONE_DUO": "iphone-duo",
                "WATCH": "watch"}
MODULES = ("release_notes", "validate_catalog", "ios_metadata", "asc_release", "ios_release", "export_ios_listing",
           "android_release", "play_listing")


def configure(config, root: Path) -> None:
    from store_release.flows.verceltics import (  # noqa: PLC0415
        android_release, asc_release, ios_metadata, ios_release, play_listing, release_notes, validate_catalog,
    )

    check_catalog_groups(config, "verceltics", GROUPS)
    catalog = config.catalog
    rebind(release_notes, {"ROOT": root})
    rebind(validate_catalog, {"ROOT": root, "CATALOG": root / catalog.path})
    ios = config.ios
    android = config.android
    if ios is not None and ios.flow == "verceltics":
        require(bool(catalog.storekit), "catalog.storekit: the verceltics flow checks the catalog against it")
        listing_dir = ios.listing.dir if ios.listing else "store/metadata/ios"
        require(listing_dir.endswith("/ios"), "ios.listing.dir: the verceltics flow reads <metadata>/ios/<locale>/")
        rebind(ios_metadata, {"ROOT": root, "LISTING_DIR": root / listing_dir, "PRIMARY_LOCALE": ios.locale})
        dirs = set_dirs(config, "verceltics")
        if dirs:
            for display, folder in FOLDER_NAMES.items():
                require(dirs[display].rsplit("/", 1)[-1] == folder,
                        f"ios.screenshots.sets: the verceltics flow stages {display} from a folder named {folder}")
            sets = tuple(dataclasses.replace(s, directory=root / dirs[s.display_type])
                         for s in ios_metadata.SCREENSHOT_SETS)
            optional = tuple(dataclasses.replace(s, directory=root / dirs[s.display_type])
                             for s in ios_metadata.OPTIONAL_SCREENSHOT_SETS)
            rebind(ios_metadata, {"SCREENSHOT_SETS": sets, "OPTIONAL_SCREENSHOT_SETS": optional,
                                  "WATCH_SCREENSHOTS": root / dirs["WATCH"]})
        rebind(asc_release, {"ROOT": root, "DEFAULT_BUNDLE_ID": ios.bundle_id, "DEFAULT_APP_ID": ios.asc_app_id,
                             "DEFAULT_CREATIVE_DIR": root / (ios.creative_dir or "store/creative/ios")})
        rebind(ios_release, {"ROOT": root, "PBXPROJ": root / ios.pbxproj, "CATALOG": root / catalog.path,
                             "BUNDLE_ID": ios.bundle_id, "TAG_PREFIX": ios.tag_prefix})
        rebind(validate_catalog, {"STOREKIT": root / catalog.storekit, "PACKAGE": ios.bundle_id})
    if android is not None and android.flow == "verceltics":
        require(bool(catalog.products_source),
                "catalog.products_source: the verceltics flow checks the Android catalog against BillingModels.kt")
        if ios is not None:
            require(android.package == ios.bundle_id,
                    "android.package: the verceltics flow's catalog uses one id for both platforms (ios.bundle_id)")
        play_dir = android.listing.dir if android.listing else "store/metadata/play"
        rebind(android_release, {"ROOT": root, "GRADLE": root / android.gradle_file, "PACKAGE_NAME": android.package,
                                 "TAG_PREFIX": android.tag_prefix,
                                 "EXPECTED_UPLOAD_CERT_SHA256": android.upload_cert_sha256})
        rebind(play_listing, {"ROOT": root, "PACKAGE_NAME": android.package, "LISTING_DIR": root / play_dir,
                              "SCREENSHOTS_DIR": root / (android.screenshots_dir or "store/google-play/screenshots/phone"),
                              "PRIMARY_LOCALE": android.locale})
        rebind(validate_catalog, {"PRODUCTS_SOURCE": root / catalog.products_source, "PACKAGE": android.package})
