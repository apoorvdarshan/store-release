"""Scowld's release flow (apoorvdarshan/scowld scripts/store, ported unchanged): see store_release.flows."""
from __future__ import annotations

from pathlib import Path

from store_release.flows import require
from store_release.flows.rebind import rebind

MODULES = ("asc_release", "ios_release", "asc_export_listing")


def configure(config, root: Path) -> None:
    from store_release.flows.scowld import asc_release, ios_release  # noqa: PLC0415

    ios = config.ios
    require(config.catalog is None, "the scowld flow submits no products: remove [catalog]")
    listing_dir = ios.listing.dir if ios.listing else "store/metadata/ios"
    rebind(asc_release, {
        "ROOT": root,
        "DEFAULT_BUNDLE_ID": ios.bundle_id,
        "DEFAULT_APP_ID": ios.asc_app_id,
        "METADATA_DIR": root / listing_dir,
        "DEFAULT_CREATIVE_DIR": root / (ios.creative_dir or "store/creative/ios"),
        "CREATIVE_LOCALE": ios.locale,
        "SCREENSHOT_MANIFEST": ios.screenshot_manifest or "screenshots.json",
    })
    rebind(ios_release, {
        "ROOT": root,
        "PBXPROJ": root / ios.pbxproj,
        "BUNDLE_ID": ios.bundle_id,
        "TAG_PREFIX": ios.tag_prefix,
    })
