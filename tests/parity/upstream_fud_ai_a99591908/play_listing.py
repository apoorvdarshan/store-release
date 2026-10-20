#!/usr/bin/env python3
"""Google Play listing text and phone screenshots (gated in CI)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "com.apoorvdarshan.calorietracker"


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def load_service_account_info() -> dict:
    raw = os.environ.get("PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        fail("PLAY_SERVICE_ACCOUNT_JSON is not set")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"PLAY_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}")


def build_android_publisher(credentials_info: dict):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


def validate_local_inputs(
    *,
    upload_listing: bool,
    upload_screenshots: bool,
    metadata_dir: Path,
    screenshots_dir: Path,
    locale: str,
) -> None:
    if upload_listing:
        loc_dir = metadata_dir / "play" / locale
        if not loc_dir.is_dir():
            fail(f"Play metadata dir missing: {loc_dir}")
        for name in ("title.txt", "short_description.txt", "full_description.txt"):
            if read_text(loc_dir / name) is None:
                fail(f"missing Play listing field: {loc_dir / name}")
    if upload_screenshots:
        if not screenshots_dir.is_dir():
            fail(f"screenshots dir missing: {screenshots_dir}")
        if not any(screenshots_dir.glob("*.png")):
            fail(f"no PNG files in {screenshots_dir}")


def upload_play_store_listing(
    service,
    *,
    package_name: str,
    locale: str,
    metadata_dir: Path,
    screenshots_dir: Path,
    upload_listing: bool,
    upload_screenshots: bool,
) -> None:
    from googleapiclient.http import MediaFileUpload

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]
    print(f"  opened Play edit {edit_id}")

    try:
        if upload_listing:
            loc_dir = metadata_dir / "play" / locale
            listing_body = {
                "title": read_text(loc_dir / "title.txt"),
                "shortDescription": read_text(loc_dir / "short_description.txt"),
                "fullDescription": read_text(loc_dir / "full_description.txt"),
            }
            service.edits().listings().update(
                packageName=package_name,
                editId=edit_id,
                language=locale,
                body=listing_body,
            ).execute()
            print(f"  updated Play listing ({locale})")

        if upload_screenshots:
            service.edits().images().deleteall(
                packageName=package_name,
                editId=edit_id,
                language=locale,
                imageType="phoneScreenshots",
            ).execute()
            print(f"  cleared existing Play phone screenshots ({locale})")
            pngs = sorted(screenshots_dir.glob("*.png"))
            for path in pngs:
                media = MediaFileUpload(str(path), mimetype="image/png", resumable=False)
                service.edits().images().upload(
                    packageName=package_name,
                    editId=edit_id,
                    language=locale,
                    imageType="phoneScreenshots",
                    media_body=media,
                ).execute()
                print(f"  uploaded Play phone screenshot {path.name}")

        service.edits().commit(packageName=package_name, editId=edit_id).execute()
        print("  committed Play edit")
    except Exception:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=ROOT / "store" / "metadata",
    )
    parser.add_argument(
        "--screenshots-dir",
        type=Path,
        default=ROOT / "store" / "metadata" / "screenshots" / "play" / "phone",
    )
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--package-name", default=PACKAGE_NAME)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate flags and local files only — no Play API calls",
    )
    args = parser.parse_args()

    do_listing = truthy("UPLOAD_LISTING")
    do_screenshots = truthy("UPLOAD_SCREENSHOTS")

    if not (do_listing or do_screenshots):
        print("Play listing skipped — UPLOAD_LISTING / UPLOAD_SCREENSHOTS are off")
        return

    validate_local_inputs(
        upload_listing=do_listing,
        upload_screenshots=do_screenshots,
        metadata_dir=args.metadata_dir,
        screenshots_dir=args.screenshots_dir,
        locale=args.locale,
    )

    if args.dry_run:
        print("Play listing dry-run — local inputs OK; no API calls")
        print(
            f"  package={args.package_name} listing={do_listing} "
            f"screenshots={do_screenshots}"
        )
        return

    credentials_info = load_service_account_info()
    service = build_android_publisher(credentials_info)
    print("uploading Play listing / screenshots…")
    upload_play_store_listing(
        service,
        package_name=args.package_name,
        locale=args.locale,
        metadata_dir=args.metadata_dir,
        screenshots_dir=args.screenshots_dir,
        upload_listing=do_listing,
        upload_screenshots=do_screenshots,
    )
    print("Play listing step finished")


if __name__ == "__main__":
    main()
