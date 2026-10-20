#!/usr/bin/env python3
"""Export ABYSM's live App Store listing into store/metadata/ios/<locale>/ (read-only).

Run it after editing the listing in App Store Connect so the repo stays the
source of truth. It only sends GET requests. Needs the same credentials as the
release workflow (APP_STORE_CONNECT_API_KEY_ID / _ISSUER_ID / _API_KEY_P8) and
PyJWT (requirements-store.txt):

  python3 scripts/store/export_listing.py            # the version on the App Store
  python3 scripts/store/export_listing.py --version 1.0.1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asc_release  # noqa: E402

LIVE_VERSION_STATES = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION"}


def pick_version(versions: list[dict[str, Any]], wanted: str | None) -> dict[str, Any]:
    if wanted:
        matches = [v for v in versions if (v.get("attributes") or {}).get("versionString") == wanted]
    else:
        matches = [v for v in versions if asc_release.version_state(v) in LIVE_VERSION_STATES]
    if len(matches) != 1:
        label = wanted or "live"
        asc_release.fail(f"expected one {label} iOS version, found {len(matches)}")
    return matches[0]


def pick_app_info(infos: list[dict[str, Any]], *, live: bool) -> dict[str, Any]:
    def state(info: dict[str, Any]) -> str:
        attrs = info.get("attributes") or {}
        return attrs.get("appStoreState") or attrs.get("state") or ""

    if live:
        matches = [i for i in infos if state(i) in LIVE_VERSION_STATES]
    else:
        matches = [i for i in infos if state(i) in asc_release.EDITABLE_APP_INFO_STATES]
    if len(matches) != 1:
        asc_release.fail(f"expected one {'live' if live else 'editable'} App Info, found {len(matches)}")
    return matches[0]


def write_fields(loc_dir: Path, attributes: dict[str, Any], fields: tuple[tuple[str, str, Any], ...]) -> list[str]:
    """Write each non-empty field; remove files for fields that are now empty."""
    written = []
    for attribute, filename, _ in fields:
        value = (attributes.get(attribute) or "").strip()
        path = loc_dir / filename
        if value:
            path.write_text(value + "\n", encoding="utf-8")
            written.append(filename)
        elif path.exists():
            path.unlink()
    return written


def export(client: Any, *, out_dir: Path, version_string: str | None) -> None:
    app_id = asc_release.find_app(client, asc_release.DEFAULT_BUNDLE_ID)
    versions = asc_release.iter_collection(
        client, f"/apps/{app_id}/appStoreVersions?limit=50&filter%5Bplatform%5D=IOS"
    )
    version = pick_version(versions, version_string)
    live = asc_release.version_state(version) in LIVE_VERSION_STATES
    print(f"exporting {version['attributes']['versionString']} ({asc_release.version_state(version)})")
    infos = asc_release.iter_collection(client, f"/apps/{app_id}/appInfos")
    info = pick_app_info(infos, live=live)
    version_locs = asc_release.iter_collection(
        client, f"/appStoreVersions/{version['id']}/appStoreVersionLocalizations?limit=50"
    )
    info_locs = {
        (row.get("attributes") or {}).get("locale"): row.get("attributes") or {}
        for row in asc_release.iter_collection(client, f"/appInfos/{info['id']}/appInfoLocalizations?limit=50")
    }
    ios_dir = out_dir / "ios"
    for row in version_locs:
        attrs = row.get("attributes") or {}
        locale = attrs.get("locale")
        if not locale:
            continue
        loc_dir = ios_dir / locale
        loc_dir.mkdir(parents=True, exist_ok=True)
        written = write_fields(loc_dir, attrs, asc_release.LISTING_FIELDS + (asc_release.WHATS_NEW_FIELD,))
        written += write_fields(loc_dir, info_locs.get(locale, {}), asc_release.APP_INFO_FIELDS)
        print(f"  {locale}: {', '.join(written)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", help="Version to export (default: the one on the App Store)")
    parser.add_argument("--out", type=Path, default=asc_release.METADATA_DIR)
    args = parser.parse_args()
    export(asc_release.client_from_environment(), out_dir=args.out, version_string=args.version)


if __name__ == "__main__":
    main()
