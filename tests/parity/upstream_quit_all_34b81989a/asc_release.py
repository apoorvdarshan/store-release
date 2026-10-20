#!/usr/bin/env python3
"""App Store Connect listing, What's New, screenshots, creative assets and submit-for-review (gated in CI).

The listing lives in the repo (store/metadata/ios/<locale>/): every locale
directory there is uploaded, and App Store Connect must already have that
locale on the version. App info fields (name, subtitle, privacy URL) are kept
for reference only and are not uploaded.

Metadata steps are idempotent: the live text and screenshots are read first and
only what differs is written. A rerun therefore also works after a review
submission was started (READY_FOR_REVIEW still takes text changes, not
screenshots) or completed (WAITING_FOR_REVIEW takes no changes): it passes when
the metadata is already in place and fails, before any write, when a change
would be needed.

Screenshots are uploaded from RGB copies made in a temporary folder: App Store
Connect refuses a PNG that stores an alpha channel, even when every pixel is
opaque. A file that stores alpha is laid over black (colour profile kept); the
committed files are only read.

Product page header and search results images (UPLOAD_CREATIVE_ASSETS) live in
the app's Asset Library. A new image is uploaded once, sent to App Review in a
review submission of its own (never the version's draft), and placed on the
version's en-US localization once Apple approves it. Without images in
store/creative/ios/ there is nothing to do.

Submitting for review covers both the App Store version and any Ready-to-Submit
in-app purchases or subscriptions listed in store/catalog/products.json. They
are attached to the same draft review submission as the version (subscriptions
through the `subscriptionVersion` relationship, one-time purchases through
`inAppPurchaseVersion`), so everything goes to App Review with one submit, after
the final build recheck. (A rerun that finds the version already waiting for
review with this run's build sends a late product in a product-only draft.) A
first-time subscription must go out with the version or App Review rejects the
build.

`--create-version` (IOS_CREATE_VERSION) runs first on a release run: it adds the
App Store version for the tag when App Store Connect does not have it yet, and
leaves an existing one alone. It refuses when another version is still on its
way to the store or the tag's version is not newer than every existing one.

`--preflight` runs before anything is built: read-only checks (GET requests
only) that the App Store version exists, is in a state the enabled switches can
use, and has every repository locale, so a release fails before it uploads."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ASC_BASE = "https://api.appstoreconnect.apple.com/v1"
# In-app purchase versions are listed under the v2 inAppPurchases resource.
ASC_V2_BASE = "https://api.appstoreconnect.apple.com/v2"
DEFAULT_BUNDLE_ID = "com.apoorvdarshan.Quit-All"
DEFAULT_APP_ID = "6760978934"
METADATA_DIR = ROOT / "store" / "metadata"
CATALOG = ROOT / "store" / "catalog" / "products.json"

# App Store Connect allows 10 screenshots per display type. Portrait sizes it
# accepts for each display type this app uses.
SCREENSHOT_SET_MAX = 10
# Apple Watch screenshots: each size belongs to one Watch display type (Apple's
# screenshot specifications), so a Watch frame is listed in screenshots.json
# under the display type its size picks. Without Watch frames there, the live
# Watch sets are left alone.
WATCH_DISPLAY_TYPES = {
    (416, 496): "APP_WATCH_SERIES_10",  # Series 10 and 11
    (422, 514): "APP_WATCH_ULTRA",  # Ultra 3
    (410, 502): "APP_WATCH_ULTRA",  # Ultra and Ultra 2
    (396, 484): "APP_WATCH_SERIES_7",  # Series 7 to 9
    (368, 448): "APP_WATCH_SERIES_4",  # Series 4 to 6 and SE
    (312, 390): "APP_WATCH_SERIES_3",
}
WATCH_LABELS = {
    "APP_WATCH_SERIES_10": "Apple Watch Series 10",
    "APP_WATCH_ULTRA": "Apple Watch Ultra",
    "APP_WATCH_SERIES_7": "Apple Watch Series 7",
    "APP_WATCH_SERIES_4": "Apple Watch Series 4",
    "APP_WATCH_SERIES_3": "Apple Watch Series 3",
}
SCREENSHOT_SIZES = {
    "APP_IPHONE_67": {(1260, 2736), (1284, 2778), (1290, 2796), (1320, 2868)},
    "APP_IPHONE_65": {(1242, 2688), (1284, 2778)},
    # One set takes both screens: outer 1398x2034 and inner 2007x2853.
    # APP_IPHONE_DUO is not in Apple's OpenAPI spec yet, but App Store Connect accepts it.
    "APP_IPHONE_DUO": {(1398, 2034), (2007, 2853)},
    **{
        display_type: {size for size, kind in WATCH_DISPLAY_TYPES.items() if kind == display_type}
        for display_type in WATCH_LABELS
    },
}
SCREENSHOT_MANIFEST = "screenshots.json"
WHATS_NEW_VERSION_FILE = "whats_new_version.txt"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# PNG chunks that describe colour, copied from a screenshot to its RGB copy
# (Pillow writes an ICC profile, iCCP, itself).
PNG_COLOUR_CHUNKS = (b"sRGB", b"gAMA", b"cHRM", b"cICP")

# Product page creative assets: the folder under --creative-dir, the placement
# type, a label for logs, and the formats Apple takes. Each folder holds at most
# one image; a slot without an image is left alone.
HEADER_PLACEMENT = "PRODUCT_PAGE_HEADER_ASSET"
SEARCH_PLACEMENT = "APP_STORE_SEARCH_RESULTS_ASSET"
CREATIVE_SLOTS = (
    ("header", HEADER_PLACEMENT, "product page header", ("png",)),
    ("search-results", SEARCH_PLACEMENT, "search results", ("png", "jpeg")),
)
CREATIVE_EXTENSIONS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
# Apple's image specs: the header is a 3840x1646 PNG; search results images are
# 3:2 PNG or JPEG from 1920x1280 to 3840x2560.
HEADER_SIZE = (3840, 1646)
SEARCH_RESULTS_WIDTHS = (1920, 3840)
CREATIVE_PLACEMENT_GROUP = "DEFAULT_PROFILE"
DEFAULT_CREATIVE_DIR = ROOT / "store" / "creative" / "ios"
# The version localization the images are placed on.
CREATIVE_LOCALE = "en-US"
# Asset Library image states (AppAssetLibraryAssetState). AWAITING_UPLOAD is the
# reservation an interrupted upload leaves behind: like a failed or archived
# image it is ignored, and the image is uploaded again.
CREATIVE_DEAD_STATES = {"AWAITING_UPLOAD", "FAILED", "ARCHIVED", "REJECTED"}
# Uploaded and still being processed; App Review takes it once it is ready.
CREATIVE_PROCESSING_STATES = {"UPLOAD_COMPLETE", "COMPLETE"}
# Processed, not yet submitted (READY_FOR_REVIEW: in a draft review submission).
CREATIVE_READY_STATES = {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW"}
CREATIVE_PENDING_STATES = {"WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED"}
# Processing a freshly uploaded image usually takes seconds.
CREATIVE_PROCESSING_TIMEOUT_SECONDS = 600
CREATIVE_POLL_SECONDS = 5
# A review submission item's relationships, requested explicitly (App Store
# Connect leaves them out otherwise).
DRAFT_ITEM_INCLUDE = "subscriptionVersion,appStoreVersion,inAppPurchaseVersion,appAssetLibraryImage"

# Every metadata field, screenshots included, can be written in these states.
EDITABLE_VERSION_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "REJECTED",
    "METADATA_REJECTED",
    "INVALID_BINARY",
}
# READY_FOR_REVIEW means the version already sits in a draft review submission
# (for example after a failed submit); it can still be submitted, and Apple still
# accepts text changes then, but not new screenshots.
SUBMITTABLE_VERSION_STATES = EDITABLE_VERSION_STATES | {"READY_FOR_REVIEW"}
TEXT_EDITABLE_VERSION_STATES = EDITABLE_VERSION_STATES | {"READY_FOR_REVIEW"}
# A rerun may find the version here after its earlier attempt started or
# completed the submission. Metadata switches pass only when nothing would change.
UNCHANGED_METADATA_STATES = {"READY_FOR_REVIEW", "WAITING_FOR_REVIEW"}
# Versions in these states are finished. Any other state means the version is
# still on its way to the store, and App Store Connect allows only one such
# version per platform, so no new version is created next to it.
FINISHED_VERSION_STATES = {
    "READY_FOR_SALE",
    "REPLACED_WITH_NEW_VERSION",
    "REMOVED_FROM_SALE",
    "DEVELOPER_REMOVED_FROM_SALE",
}


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


class AscHttpError(SystemExit):
    """An App Store Connect HTTP error; exits like fail() unless a caller retries."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"error: {message}")
        self.status = status


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def attrs(row: dict[str, Any] | None) -> dict[str, Any]:
    return (row or {}).get("attributes") or {}


def related_id(row: dict[str, Any], name: str) -> str | None:
    """The id a JSON:API row's `name` relationship points at, if it was included."""
    related = (((row.get("relationships") or {}).get(name) or {}).get("data")) or {}
    return related.get("id") if isinstance(related, dict) else None


def png_chunks(raw: bytes, *, strict: bool = True) -> list[tuple[bytes, bytes]]:
    """(type, data) of every chunk of a PNG, up to IEND.

    Raises ValueError when the bytes are not a PNG with an IHDR header. Strict
    (the default) also requires a well-formed file: every chunk complete with a
    matching CRC, image data that decompresses to the rows the header describes,
    and a final IEND, so a truncated or corrupt file fails before anything is
    uploaded. Lenient reading (the manifest's size check, and staging without
    Pillow, which only need the header) stops at the first chunk that runs past
    the end of the file.
    """
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(raw):
        length = int.from_bytes(raw[offset : offset + 4], "big")
        kind = raw[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + (4 if strict else 0) > len(raw):
            if strict or not chunks:
                raise ValueError(f"truncated {kind.decode('latin-1')!r} chunk")
            break
        data = raw[start:end]
        if strict and zlib.crc32(kind + data) != int.from_bytes(raw[end : end + 4], "big"):
            raise ValueError(f"the {kind.decode('latin-1')!r} chunk fails its CRC check")
        chunks.append((kind, data))
        offset = end + 4  # data, then the CRC
        if kind == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) < 13:
        raise ValueError("no IHDR header")
    if strict:
        if chunks[-1][0] != b"IEND":
            raise ValueError("no IEND chunk (the file is truncated)")
        check_png_image_data(chunks)
    return chunks


# Samples per pixel for each PNG colour type.
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def check_png_image_data(chunks: list[tuple[bytes, bytes]]) -> None:
    """Raises ValueError unless the IDAT data is one complete zlib stream of the header's size.

    Decompresses in bounded steps; the size is checked for non-interlaced images
    (Pillow writes those), an interlaced image only needs a complete stream.
    """
    header = chunks[0][1]
    width, height = png_size(chunks)
    bit_depth, colour_type, interlace = header[8], header[9], header[12]
    if colour_type not in PNG_CHANNELS:
        raise ValueError(f"unknown PNG colour type {colour_type}")
    idat = [data for kind, data in chunks if kind == b"IDAT"]
    if not idat:
        raise ValueError("no image data (IDAT)")
    inflater = zlib.decompressobj()
    size = 0
    try:
        for data in idat:
            pending = data
            while pending:
                size += len(inflater.decompress(pending, 1 << 20))
                pending = inflater.unconsumed_tail
        size += len(inflater.flush())
    except zlib.error as exc:
        raise ValueError(f"the image data does not decompress ({exc})") from None
    if not inflater.eof:
        raise ValueError("the image data ends early (the file is truncated)")
    if interlace == 0:
        expected = height * (1 + (width * PNG_CHANNELS[colour_type] * bit_depth + 7) // 8)
        if size != expected:
            raise ValueError(
                f"the image data holds {size} bytes, not the {expected} a {width}x{height} image needs"
            )


def png_size(chunks: list[tuple[bytes, bytes]]) -> tuple[int, int]:
    header = chunks[0][1]
    return int.from_bytes(header[0:4], "big"), int.from_bytes(header[4:8], "big")


def png_stores_alpha(chunks: list[tuple[bytes, bytes]]) -> bool:
    """True when a PNG stores alpha: grey or RGB with alpha (colour type 4 or 6), or a tRNS chunk.

    App Store Connect refuses such files even when every pixel is opaque.
    """
    colour_type = chunks[0][1][9]
    return colour_type in (4, 6) or any(kind == b"tRNS" for kind, _ in chunks)


def jpeg_size(raw: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's start-of-frame segment, or None when there is none."""
    if raw[:3] != b"\xff\xd8\xff":
        return None
    offset = 2
    while offset + 9 < len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        marker = raw[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        # SOF0-SOF15, except DHT (C4), JPG (C8) and DAC (CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(raw[offset + 5 : offset + 7], "big")
            width = int.from_bytes(raw[offset + 7 : offset + 9], "big")
            return width, height
        offset += 2 + int.from_bytes(raw[offset + 2 : offset + 4], "big")
    return None


def staged_png(path: Path, label: str) -> tuple[int, int]:
    """The size of a staged screenshot; fails unless it is a PNG without alpha.

    The last guard before upload: staging (stage_screenshots) writes RGB copies,
    so a file that still stores alpha was not staged by it.
    """
    try:
        chunks = png_chunks(path.read_bytes())
    except ValueError as exc:
        fail(f"the {label} screenshot {path.name} is not a readable PNG ({exc})")
    if png_stores_alpha(chunks):
        fail(
            f"the {label} screenshot {path.name} stores an alpha channel (PNG colour type 4 or 6, "
            "or a tRNS chunk), which App Store Connect refuses; upload the RGB copy stage_screenshots "
            "makes instead"
        )
    return png_size(chunks)


class AscClient:
    # JWTs are minted for 20 minutes; refresh well before that during long waits.
    TOKEN_MAX_AGE_SECONDS = 600

    def __init__(
        self, token: str | None = None, *, token_factory: Any | None = None
    ) -> None:
        if token is None and token_factory is None:
            raise ValueError("AscClient needs a token or a token_factory")
        self._token_factory = token_factory
        self._token = token if token is not None else token_factory()
        self._token_minted = time.monotonic()

    def _authorization(self) -> str:
        if (
            self._token_factory is not None
            and time.monotonic() - self._token_minted > self.TOKEN_MAX_AGE_SECONDS
        ):
            self._token = self._token_factory()
            self._token_minted = time.monotonic()
        return f"Bearer {self._token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> dict[str, Any] | None:
        url = path if path.startswith("http") else f"{ASC_BASE}{path}"
        headers = {
            "Authorization": self._authorization(),
            "Accept": accept,
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type
        elif raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
                if not payload:
                    return None
                if accept != "application/json":
                    return {"raw": payload}
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(f"error: ASC {method} {path} → HTTP {exc.code}: {detail[:2000]}", file=sys.stderr)
            raise AscHttpError(exc.code, f"ASC {method} {path} → HTTP {exc.code}") from None

    def get(self, path: str) -> dict[str, Any]:
        result = self.request("GET", path)
        assert result is not None
        return result

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("PATCH", path, body=body)
        assert result is not None
        return result

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", path, body=body)
        assert result is not None
        return result

    def upload_bytes(
        self, url: str, data: bytes, headers: dict[str, str], *, method: str = "PUT"
    ) -> None:
        req = urllib.request.Request(
            url, data=data, method=method.upper(), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=300):
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            fail(f"ASC asset upload → HTTP {exc.code}: {detail[:1000]}")


def make_asc_token(key_id: str, issuer_id: str, private_key: str) -> str:
    import jwt

    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
    }
    headers = {"kid": key_id, "typ": "JWT"}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def load_credentials() -> tuple[str, str, str]:
    key_id = os.environ.get("APP_STORE_CONNECT_API_KEY_ID", "").strip()
    issuer_id = os.environ.get("APP_STORE_CONNECT_ISSUER_ID", "").strip()
    key_p8 = os.environ.get("APP_STORE_CONNECT_API_KEY_P8", "").strip()
    if not key_id or not issuer_id or not key_p8:
        fail(
            "missing ASC credentials — set APP_STORE_CONNECT_API_KEY_ID, "
            "APP_STORE_CONNECT_ISSUER_ID, APP_STORE_CONNECT_API_KEY_P8"
        )
    key_p8 = key_p8.replace("\\n", "\n")
    return key_id, issuer_id, key_p8


def client_from_environment() -> AscClient:
    """Client whose JWT is re-minted automatically during long-running steps."""
    key_id, issuer_id, key_p8 = load_credentials()
    return AscClient(token_factory=lambda: make_asc_token(key_id, issuer_id, key_p8))


def find_app(client: AscClient, bundle_id: str) -> str:
    """The id of the one App Store Connect app whose bundle id is exactly bundle_id.

    The filter is not trusted to be exact: rows for other bundle ids are ignored,
    and anything but exactly one exact match stops the run.
    """
    query = urllib.parse.urlencode({"filter[bundleId]": bundle_id})
    rows = iter_collection(client, f"/apps?{query}")
    matches = [row for row in rows if (row.get("attributes") or {}).get("bundleId") == bundle_id]
    if not matches:
        fail(f"no App Store Connect app has bundle id {bundle_id}")
    if len(matches) > 1:
        ids = ", ".join(str(row.get("id")) for row in matches)
        fail(f"ambiguous: {len(matches)} App Store Connect apps have bundle id {bundle_id} ({ids})")
    return matches[0]["id"]


def list_ios_app_store_versions(client: AscClient, app_id: str) -> list[dict[str, Any]]:
    # Prefer the app relationship listing — some ASC API keys cannot
    # GET_COLLECTION on /appStoreVersions with filter[app]=… (403).
    rows: list[dict[str, Any]] = []
    path: str | None = (
        f"/apps/{app_id}/appStoreVersions?"
        + urllib.parse.urlencode({"limit": "50", "filter[platform]": "IOS"})
    )
    while path:
        payload = client.get(path)
        rows.extend(payload.get("data") or [])
        next_url = (payload.get("links") or {}).get("next")
        if not next_url:
            break
        path = next_url
    return rows


def get_ios_app_store_version(
    client: AscClient, app_id: str, version_string: str
) -> dict[str, Any]:
    matches = [
        row
        for row in list_ios_app_store_versions(client, app_id)
        if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if not matches:
        fail(
            f"no IOS App Store version {version_string!r} for app {app_id} "
            "(turn on IOS_CREATE_VERSION, or create the version in App Store Connect first)"
        )
    if len(matches) > 1:
        fail(
            f"ambiguous App Store versions for {version_string!r} on IOS — "
            f"got {len(matches)} records"
        )
    return matches[0]


def localization_for_locale(
    client: AscClient, collection_path: str, locale: str
) -> dict[str, Any]:
    query = urllib.parse.urlencode({"filter[locale]": locale, "limit": "1"})
    payload = client.get(f"{collection_path}?{query}")
    data = payload.get("data") or []
    if not data:
        fail(
            f"App Store Connect has no {locale} localization at {collection_path}; "
            "add that language to the version in App Store Connect first"
        )
    return data[0]


# Version-localization fields: (App Store Connect attribute, file, max characters).
LISTING_FIELDS = (
    ("description", "description.txt", 4000),
    ("keywords", "keywords.txt", 100),
    ("promotionalText", "promotional_text.txt", 170),
    ("marketingUrl", "marketing_url.txt", None),
    ("supportUrl", "support_url.txt", None),
)
WHATS_NEW_FIELD = ("whatsNew", "whats_new.txt", 4000)
URL_ATTRIBUTES = {"marketingUrl", "supportUrl"}


def listing_locales(metadata_dir: Path) -> list[str]:
    """Locales kept in the repo: every store/metadata/ios/<locale>/ directory."""
    ios_dir = metadata_dir / "ios"
    if not ios_dir.is_dir():
        return []
    return sorted(
        child.name
        for child in ios_dir.iterdir()
        if child.is_dir() and re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]+)*", child.name)
    )


def check_listing_text(loc_dir: Path, fields: tuple[tuple[str, str, int | None], ...]) -> list[str]:
    """Problems with the listing files for one locale (missing, too long, bad URL)."""
    problems = []
    for attribute, filename, limit in fields:
        value = read_text(loc_dir / filename)
        if value is None:
            problems.append(f"{loc_dir / filename} is missing or empty")
            continue
        if limit is not None and len(value) > limit:
            problems.append(f"{loc_dir / filename} has {len(value)} characters (max {limit})")
        if attribute in URL_ATTRIBUTES and not re.fullmatch(r"https?://\S+", value):
            problems.append(f"{loc_dir / filename} is not an http(s) URL: {value!r}")
    return problems


def wanted_version_text(
    loc_dir: Path, *, include_listing: bool, include_whats_new: bool
) -> dict[str, str]:
    """Version-localization fields the enabled switches upload for one locale.

    Listing text and What's New are separate gates; app info fields (name,
    subtitle, privacy URL) are never uploaded.
    """
    fields = (LISTING_FIELDS if include_listing else ()) + (
        (WHATS_NEW_FIELD,) if include_whats_new else ()
    )
    wanted: dict[str, str] = {}
    for field, filename, _limit in fields:
        value = read_text(loc_dir / filename)
        if value:
            wanted[field] = value
    return wanted


def normalized_text(value: Any) -> str:
    """Text as compared with App Store Connect (line endings and outer whitespace ignored)."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").strip()


def file_md5(path: Path) -> str:
    """The MD5 App Store Connect keeps as a screenshot's sourceFileChecksum."""
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


class LocalePlan:
    """What the metadata step would write for one locale, read with GET requests only."""

    def __init__(self, locale: str, localization_id: str) -> None:
        self.locale = locale
        self.localization_id = localization_id
        # Version-localization fields the switches upload, and the ones that differ.
        self.text_fields: list[str] = []
        self.text_changes: dict[str, str] = {}
        # Screenshot display types the switch uploads, with the files that are
        # sent (the RGB copies stage_screenshots made), and the ones that differ.
        self.screenshots: dict[str, list[Path]] = {}
        self.stale_screenshots: list[str] = []


def plan_metadata(
    client: AscClient,
    *,
    version_id: str,
    locales: list[str],
    metadata_dir: Path,
    include_listing: bool,
    include_whats_new: bool,
    screenshot_sets: dict[str, dict[str, list[Path]]],
    copies: dict[Path, Path] | None = None,
) -> list[LocalePlan]:
    """Compare the repository metadata with App Store Connect without changing anything.

    copies maps each committed screenshot to the RGB copy that is uploaded in
    its place (stage_screenshots); a live set holding either the copies or the
    committed files is up to date. Fails if App Store Connect lacks a repository
    locale on the version.
    """
    plans: list[LocalePlan] = []
    for locale in locales:
        sets = screenshot_sets.get(locale) or {}
        if not (include_listing or include_whats_new or sets):
            continue
        loc = localization_for_locale(
            client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
        )
        plan = LocalePlan(locale, loc["id"])
        wanted = wanted_version_text(
            metadata_dir / "ios" / locale,
            include_listing=include_listing,
            include_whats_new=include_whats_new,
        )
        live = loc.get("attributes") or {}
        plan.text_fields = list(wanted)
        plan.text_changes = {
            field: value
            for field, value in wanted.items()
            if normalized_text(live.get(field)) != normalized_text(value)
        }
        plan.screenshots = {
            display_type: [copies[path] for path in pngs] if copies is not None else list(pngs)
            for display_type, pngs in sets.items()
        }
        plan.stale_screenshots = [
            display_type
            for display_type, pngs in sets.items()
            if not screenshots_up_to_date(
                client, loc["id"], display_type, plan.screenshots[display_type], committed=pngs
            )
        ]
        plans.append(plan)
    return plans


def metadata_problems(plans: list[LocalePlan], *, version: str, state: str) -> list[str]:
    """Changes the version's state cannot take; empty when the plan can be applied."""
    problems: list[str] = []
    for plan in plans:
        if plan.text_changes and state not in TEXT_EDITABLE_VERSION_STATES:
            problems.append(
                f"{plan.locale}: {', '.join(plan.text_changes)} differ from App Store Connect, "
                f"and version {version!r} takes no text changes while {state}"
            )
        if plan.stale_screenshots and state not in EDITABLE_VERSION_STATES:
            problems.append(
                f"{plan.locale}: the {', '.join(plan.stale_screenshots)} screenshots differ from "
                f"App Store Connect, and version {version!r} takes no new screenshots while {state}"
            )
    return problems


def apply_metadata(client: AscClient, plans: list[LocalePlan]) -> None:
    """Write only what differs: changed text fields and stale screenshot sets."""
    # The last guard, before the first write: every screenshot that is about to
    # be uploaded must be a complete PNG without alpha (an RGB copy).
    for plan in plans:
        for display_type in plan.stale_screenshots:
            for path in plan.screenshots[display_type]:
                staged_png(path, f"{plan.locale} {display_type}")
    for plan in plans:
        if plan.text_changes:
            client.patch(
                f"/appStoreVersionLocalizations/{plan.localization_id}",
                {
                    "data": {
                        "type": "appStoreVersionLocalizations",
                        "id": plan.localization_id,
                        "attributes": plan.text_changes,
                    }
                },
            )
            print(f"  updated version localization ({plan.locale}): {', '.join(plan.text_changes)}")
        elif plan.text_fields:
            print(f"  version localization ({plan.locale}) already up to date: {', '.join(plan.text_fields)}")
        for display_type, pngs in plan.screenshots.items():
            if display_type in plan.stale_screenshots:
                print(f"  {plan.locale} {display_type}: {len(pngs)} screenshot(s)")
                replace_screenshot_set(client, plan.localization_id, display_type, pngs)
            else:
                print(f"  {plan.locale} {display_type}: already up to date ({len(pngs)} screenshot(s))")


def load_screenshot_sets(metadata_dir: Path, locale: str, root: Path | None = None) -> dict[str, list[Path]]:
    """Ordered screenshots per display type for one locale, from its screenshots.json.

    Paths in the manifest are relative to the repository root (ROOT unless
    given). Fails with a clear message if the manifest, a file, or an image
    size is wrong.
    """
    root = ROOT if root is None else root
    manifest_path = metadata_dir / "ios" / locale / SCREENSHOT_MANIFEST
    if not manifest_path.is_file():
        fail(
            f"screenshot upload is on, but {manifest_path} is missing; list the App Store "
            "screenshots for each display type there (see store/README.md)"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{manifest_path} is not valid JSON: {exc}")
    if not isinstance(manifest, dict) or not manifest:
        fail(f"{manifest_path} must map display types to lists of PNG paths")

    sets: dict[str, list[Path]] = {}
    for display_type, entries in manifest.items():
        accepted = SCREENSHOT_SIZES.get(display_type)
        if accepted is None:
            fail(f"{manifest_path}: unsupported display type {display_type!r}")
        if not isinstance(entries, list) or not entries:
            fail(f"{manifest_path}: {display_type} needs at least one screenshot")
        if len(entries) > SCREENSHOT_SET_MAX:
            fail(f"{manifest_path}: {display_type} has {len(entries)} screenshots (max {SCREENSHOT_SET_MAX})")
        paths: list[Path] = []
        for entry in entries:
            if not isinstance(entry, str) or not entry or entry.startswith("/") or ".." in Path(entry).parts:
                fail(f"{manifest_path}: {entry!r} must be a path relative to the repository root")
            path = root / entry
            if not path.is_file():
                fail(f"{manifest_path}: screenshot {entry} does not exist")
            try:
                # The header is enough here; each upload copy is checked in full.
                size = png_size(png_chunks(path.read_bytes(), strict=False))
            except ValueError:
                fail(f"{manifest_path}: {entry} is not a PNG")
            if size not in accepted:
                allowed = ", ".join(f"{w}x{h}" for w, h in sorted(accepted))
                # The size picks the display type: name the one this frame belongs to.
                others = [kind for kind, sizes in SCREENSHOT_SIZES.items() if size in sizes]
                hint = f" ({size[0]}x{size[1]} is an {' or '.join(others)} size)" if others else ""
                fail(
                    f"{manifest_path}: {entry} is {size[0]}x{size[1]}; {display_type} accepts "
                    f"{allowed}{hint}"
                )
            paths.append(path)
        if len(set(paths)) != len(paths):
            fail(f"{manifest_path}: {display_type} lists a screenshot twice")
        sets[display_type] = paths
    return sets


def try_import_pillow():
    try:
        from PIL import Image  # type: ignore[import-untyped]

        return Image
    except ImportError:
        return None


def opaque_rgb(Image, img):
    """The image as RGB, with transparent pixels laid over black."""
    rgba = img.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, (0, 0, 0))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def colour_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """The source PNG's colour chunks other than its ICC profile (Pillow keeps that one)."""
    try:
        chunks = png_chunks(raw, strict=False)
    except ValueError:
        return []
    return [(kind, data) for kind, data in chunks if kind in PNG_COLOUR_CHUNKS]


def prepare_asc_png(Image, raw: bytes) -> bytes:
    """An RGB PNG of the screenshot at its own size, without alpha, colour profile kept."""
    from PIL import PngImagePlugin  # type: ignore[import-untyped]

    with Image.open(io.BytesIO(raw)) as img:
        icc_profile = img.info.get("icc_profile")
        # Flattened once, so translucent pixels are blended over black a single time.
        out_img = opaque_rgb(Image, img)
    info = PngImagePlugin.PngInfo()
    for kind, data in colour_chunks(raw):
        info.add(kind, data)
    out = io.BytesIO()
    options = {"icc_profile": icc_profile} if icc_profile else {}
    out_img.save(out, format="PNG", pnginfo=info, **options)
    return out.getvalue()


def stage_screenshots(
    screenshot_sets: dict[str, dict[str, list[Path]]], staging_dir: Path
) -> dict[Path, Path]:
    """Make the copy of every listed screenshot that is uploaded; returns committed file -> copy.

    App Store Connect refuses a PNG that stores an alpha channel, even when every
    pixel is opaque, so a file that stores alpha is copied as RGB with Pillow
    (transparent pixels laid over black, colour profile kept); any other file is
    copied as it is. The copies go to staging_dir, a temporary folder, under
    their own file names; the committed files are only read. Without Pillow, a
    file that stores alpha stops the run. Each copy then passes the last guard
    (staged_png), so nothing that stores alpha or is not a complete PNG reaches
    an upload; all of this happens before any App Store Connect call.
    """
    copies: dict[Path, Path] = {}
    flattened = 0
    for locale, sets in screenshot_sets.items():
        for display_type, pngs in sets.items():
            label = f"{locale} {display_type}"
            for path in pngs:
                if path in copies:
                    continue
                raw = path.read_bytes()
                try:
                    stores_alpha = png_stores_alpha(png_chunks(raw, strict=False))
                except ValueError:
                    fail(f"the {label} screenshot {path.name} is not a PNG")
                if stores_alpha:
                    Image = try_import_pillow()
                    if Image is None:
                        fail(
                            f"the {label} screenshot {path.name} stores an alpha channel, which App Store "
                            "Connect refuses; install Pillow (requirements-store.txt) so the upload can use "
                            "an RGB copy, or re-export it as RGB"
                        )
                    try:
                        raw = prepare_asc_png(Image, raw)
                    except Exception as exc:  # noqa: BLE001 - Pillow raises several kinds for a bad file
                        fail(f"the {label} screenshot {path.name} is not a readable PNG ({exc})")
                    flattened += 1
                copy = staging_dir / f"{len(copies) + 1:03d}" / path.name
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(raw)
                copies[path] = copy
            for path in pngs:
                staged_png(copies[path], label)
    if copies:
        print(
            f"  staged {len(copies)} screenshot(s) for upload: {flattened} RGB copies of files that store "
            "an alpha channel (transparent pixels over black), the rest as committed"
        )
    return copies


def screenshot_sets_for_localization(
    client: AscClient, version_loc_id: str
) -> list[dict[str, Any]]:
    payload = client.get(
        f"/appStoreVersionLocalizations/{version_loc_id}/appScreenshotSets"
    )
    return payload.get("data") or []


def find_screenshot_set(
    client: AscClient, version_loc_id: str, display_type: str
) -> dict[str, Any] | None:
    for item in screenshot_sets_for_localization(client, version_loc_id):
        if (item.get("attributes") or {}).get("screenshotDisplayType") == display_type:
            return item
    return None


def screenshots_up_to_date(
    client: AscClient,
    version_loc_id: str,
    display_type: str,
    pngs: list[Path],
    committed: list[Path] | None = None,
) -> bool:
    """True when the live set already holds exactly these files, in this order (read-only).

    Compares App Store Connect's sourceFileChecksum (MD5) with the files (the
    RGB copies that would be uploaded) or, when given, with the committed files
    they were made from, which a set uploaded before the copies existed holds; a
    screenshot whose processing failed never counts as up to date.
    """
    shot_set = find_screenshot_set(client, version_loc_id, display_type)
    if shot_set is None:
        return False
    payload = client.get(f"/appScreenshotSets/{shot_set['id']}/appScreenshots")
    live: list[str] = []
    for shot in payload.get("data") or []:
        attrs = shot.get("attributes") or {}
        if (attrs.get("assetDeliveryState") or {}).get("state") == "FAILED":
            return False
        live.append(str(attrs.get("sourceFileChecksum") or "").lower())
    if live == [file_md5(path) for path in pngs]:
        return True
    return committed is not None and live == [file_md5(path) for path in committed]


def ensure_screenshot_set(
    client: AscClient, version_loc_id: str, display_type: str
) -> dict[str, Any]:
    existing = find_screenshot_set(client, version_loc_id, display_type)
    if existing is not None:
        return existing
    created = client.post(
        "/appScreenshotSets",
        {
            "data": {
                "type": "appScreenshotSets",
                "attributes": {"screenshotDisplayType": display_type},
                "relationships": {
                    "appStoreVersionLocalization": {
                        "data": {
                            "type": "appStoreVersionLocalizations",
                            "id": version_loc_id,
                        }
                    }
                },
            }
        },
    )
    return created["data"]


def list_screenshot_ids(client: AscClient, screenshot_set_id: str) -> list[str]:
    payload = client.get(f"/appScreenshotSets/{screenshot_set_id}/appScreenshots")
    return [shot["id"] for shot in payload.get("data") or []]


def delete_screenshots(client: AscClient, screenshot_ids: list[str]) -> None:
    for shot_id in screenshot_ids:
        client.request("DELETE", f"/appScreenshots/{shot_id}")


def send_upload_operations(
    client: AscClient, reserved: dict[str, Any], raw: bytes, name: str
) -> None:
    """Send a file's bytes the way the reservation's uploadOperations ask for."""
    upload_ops = attrs(reserved).get("uploadOperations") or []
    if not upload_ops:
        fail(f"ASC returned no uploadOperations for {name}")
    for op in upload_ops:
        offset = int(op.get("offset", 0))
        length = int(op["length"])
        chunk = raw[offset : offset + length]
        headers = {h["name"]: h["value"] for h in op.get("requestHeaders") or []}
        method = (op.get("method") or "PUT").upper()
        client.upload_bytes(op["url"], chunk, headers, method=method)


def upload_screenshot_file(
    client: AscClient, screenshot_set_id: str, image_path: Path
) -> None:
    raw = image_path.read_bytes()
    checksum = hashlib.md5(raw, usedforsecurity=False).hexdigest()
    reserved = client.post(
        "/appScreenshots",
        {
            "data": {
                "type": "appScreenshots",
                "attributes": {
                    "fileName": image_path.name,
                    "fileSize": len(raw),
                },
                "relationships": {
                    "appScreenshotSet": {
                        "data": {"type": "appScreenshotSets", "id": screenshot_set_id}
                    }
                },
            }
        },
    )
    shot = reserved["data"]
    shot_id = shot["id"]
    send_upload_operations(client, shot, raw, image_path.name)
    # The checksum lets a rerun recognise a set that is already up to date.
    client.patch(
        f"/appScreenshots/{shot_id}",
        {
            "data": {
                "type": "appScreenshots",
                "id": shot_id,
                "attributes": {"uploaded": True, "sourceFileChecksum": checksum},
            }
        },
    )
    print(f"  uploaded screenshot {image_path.name}")


def replace_screenshot_set(
    client: AscClient, version_loc_id: str, display_type: str, pngs: list[Path]
) -> None:
    """Replace one display type's screenshots, keeping the manifest order."""
    shot_set = ensure_screenshot_set(client, version_loc_id, display_type)
    set_id = shot_set["id"]

    # Old ASC ids still on the set; pop front when deleting for capacity or final cleanup.
    pending_old_ids = list_screenshot_ids(client, set_id)

    # Upload replacements one at a time. ASC rejects new reservations when the set is
    # at capacity, so delete one pending old screenshot before each upload when full.
    # If a mid-run upload fails, the live set may be mixed (some olds removed, some news
    # uploaded); fix the error and re-run, or restore screenshots in App Store Connect.
    for path in pngs:
        current_ids = list_screenshot_ids(client, set_id)
        if len(current_ids) >= SCREENSHOT_SET_MAX:
            if not pending_old_ids:
                fail(
                    f"ASC {display_type} screenshot set at capacity ({SCREENSHOT_SET_MAX}) "
                    f"with no tracked old screenshots left to replace"
                )
            delete_screenshots(client, [pending_old_ids.pop(0)])
        upload_screenshot_file(client, set_id, path)

    if pending_old_ids:
        delete_screenshots(client, pending_old_ids)
        print(f"  removed {len(pending_old_ids)} previous {display_type} screenshot(s)")


def iter_collection(client: AscClient, path: str) -> list[dict[str, Any]]:
    """Return every record in an ASC collection, following links.next."""
    rows: list[dict[str, Any]] = []
    while path:
        payload = client.get(path)
        rows.extend(payload.get("data") or [])
        next_url = (payload.get("links") or {}).get("next")
        if not next_url:
            break
        path = next_url
    return rows


def creative_problem(placement_type: str, path: Path, formats: tuple[str, ...]) -> str | None:
    """Why App Store Connect would refuse this image for the slot, or None when it fits."""
    raw = path.read_bytes()
    kind = "png" if raw.startswith(PNG_SIGNATURE) else "jpeg" if raw[:3] == b"\xff\xd8\xff" else None
    if kind is None:
        return "is not a PNG or JPEG file"
    if CREATIVE_EXTENSIONS[path.suffix.lower()] != kind:
        return f"holds {kind.upper()} data but is named {path.suffix}"
    if kind not in formats:
        return f"is a {kind.upper()}; this slot takes {' or '.join(f.upper() for f in formats)}"
    if kind == "png":
        try:
            chunks = png_chunks(raw)
        except ValueError as exc:
            return f"is not a readable PNG ({exc})"
        if png_stores_alpha(chunks):
            return (
                "stores an alpha channel (PNG colour type 4 or 6, or a tRNS chunk), which App Store "
                "Connect refuses; re-export it as RGB"
            )
        width, height = png_size(chunks)
    else:
        size = jpeg_size(raw)
        if size is None:
            return "is not a readable JPEG"
        width, height = size
    if placement_type == HEADER_PLACEMENT:
        if (width, height) != HEADER_SIZE:
            return f"is {width}x{height}; the header must be exactly {HEADER_SIZE[0]}x{HEADER_SIZE[1]}"
        return None
    low, high = SEARCH_RESULTS_WIDTHS
    if not (low <= width <= high and width * 2 == height * 3):
        return (
            f"is {width}x{height}; search results images are 3:2, from {low}x{low * 2 // 3} "
            f"to {high}x{high * 2 // 3}"
        )
    return None


def creative_sources(creative_dir: Path) -> list[tuple[str, str, Path]]:
    """(placement type, label, image) for every creative slot that has an image.

    Checks each image's format, size and alpha first, so a file App Store
    Connect would refuse stops the run before any call. Without any image
    there is nothing to do.
    """
    found = []
    for folder, placement_type, label, formats in CREATIVE_SLOTS:
        slot_dir = creative_dir / folder
        images = sorted(
            p
            for p in (slot_dir.iterdir() if slot_dir.is_dir() else [])
            if p.is_file() and p.suffix.lower() in CREATIVE_EXTENSIONS
        )
        if len(images) > 1:
            fail(f"the {label} slot takes one image; {slot_dir} has {len(images)}")
        if not images:
            print(f"  no {label} image in {slot_dir}; that slot is left alone")
            continue
        problem = creative_problem(placement_type, images[0], formats)
        if problem:
            fail(f"the {label} image {images[0].name} {problem}")
        found.append((placement_type, label, images[0]))
    if not found:
        print(f"  {creative_dir} has no header or search results image; nothing to do")
    return found


def asset_library_id(client: AscClient, app_id: str) -> str:
    library_id = (client.get(f"/apps/{app_id}/assetLibrary").get("data") or {}).get("id")
    if not library_id:
        fail(f"App Store Connect returned no Asset Library for app {app_id}")
    return library_id


def library_images(client: AscClient, library_id: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"filter[category]": "CREATIVE_ASSETS", "limit": "200"})
    return iter_collection(client, f"/appAssetLibraries/{library_id}/images?{query}")


def library_image_for(images: list[dict[str, Any]], path: Path) -> dict[str, Any] | None:
    """The newest usable library image uploaded from this file (same name and size).

    Reservations an interrupted upload left (AWAITING_UPLOAD), failed and
    archived images do not count, so the file is uploaded again. A file whose
    only upload App Review rejected stops the run.
    """
    size = path.stat().st_size
    same = [
        image
        for image in images
        if attrs(image).get("fileName") == path.name and attrs(image).get("fileSize") == size
    ]
    usable = [image for image in same if attrs(image).get("state") not in CREATIVE_DEAD_STATES]
    rejected = [image for image in same if attrs(image).get("state") == "REJECTED"]
    if not usable and rejected:
        fail(
            f"App Review rejected {path.name}; replace the image (with a new file name) and re-run. "
            f"Details: {json.dumps(attrs(rejected[0]).get('stateDetails'))}"
        )
    return max(usable, key=lambda image: attrs(image).get("createdDate") or "") if usable else None


def wait_for_library_image(client: AscClient, image_id: str, name: str) -> dict[str, Any]:
    """Poll an uploaded image until Apple has processed it; fails on FAILED or timeout."""
    deadline = time.monotonic() + CREATIVE_PROCESSING_TIMEOUT_SECONDS
    while True:
        image = client.get(f"/appAssetLibraryImages/{image_id}").get("data") or {}
        state = attrs(image).get("state") or ""
        asset = attrs(image).get("imageAsset") or {}
        if state == "FAILED":
            fail(
                f"Apple could not process {name} ({image_id}): "
                f"{json.dumps(attrs(image).get('stateDetails'))}"
            )
        if state in CREATIVE_READY_STATES and asset.get("width") and asset.get("height"):
            return image
        if state in CREATIVE_PENDING_STATES or state == "APPROVED":
            return image
        if time.monotonic() >= deadline:
            fail(f"Apple did not finish processing {name} ({image_id}) in time; it is {state or 'in an unknown state'}")
        print(f"  waiting for Apple to process {name} ({state or 'unknown state'})")
        time.sleep(CREATIVE_POLL_SECONDS)


def upload_library_image(client: AscClient, library_id: str, path: Path) -> dict[str, Any]:
    """Reserve, upload and commit one creative image, then wait until Apple has processed it."""
    raw = path.read_bytes()
    reserved = client.post(
        "/appAssetLibraryImages",
        {
            "data": {
                "type": "appAssetLibraryImages",
                "attributes": {
                    "fileName": path.name,
                    "fileSize": len(raw),
                    "category": "CREATIVE_ASSETS",
                },
                "relationships": {
                    "assetLibrary": {"data": {"type": "appAssetLibraries", "id": library_id}}
                },
            }
        },
    )["data"]
    image_id = reserved["id"]
    send_upload_operations(client, reserved, raw, path.name)
    client.patch(
        f"/appAssetLibraryImages/{image_id}",
        {
            "data": {
                "type": "appAssetLibraryImages",
                "id": image_id,
                "attributes": {"uploaded": True},
            }
        },
    )
    print(f"  uploaded {path.name} to the Asset Library ({image_id})")
    return wait_for_library_image(client, image_id, path.name)


class CreativeStep:
    """What one creative slot needs, decided before anything is written.

    action: upload (then review), review, pending (in App Review), place,
    placed, or locked (approved, but the version cannot be edited).
    """

    def __init__(
        self,
        placement_type: str,
        label: str,
        path: Path,
        image: dict[str, Any] | None,
        action: str,
        swap_out: dict[str, Any] | None = None,
    ) -> None:
        self.placement_type = placement_type
        self.label = label
        self.path = path
        self.image = image
        self.action = action
        # The localization's placement in this slot that holds another image.
        self.swap_out = swap_out


class CreativePlan:
    """The steps plan_creative_assets decided; no steps (and no ids) when there are no images."""

    def __init__(self, library_id: str | None, localization_id: str | None, steps: list[CreativeStep]) -> None:
        self.library_id = library_id
        self.localization_id = localization_id
        self.steps = steps


def image_placement_ids(client: AscClient, image_id: str) -> set[str]:
    return {
        row["id"]
        for row in iter_collection(client, f"/appAssetLibraryImages/{image_id}/relationships/placements?limit=50")
    }


def slot_placements(client: AscClient, localization_id: str, placement_type: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"filter[placementType]": placement_type, "limit": "50"})
    rows = iter_collection(client, f"/appStoreVersionLocalizations/{localization_id}/placements?{query}")
    return [row for row in rows if attrs(row).get("placementType", placement_type) == placement_type]


def plan_creative_assets(
    client: AscClient,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    creative_dir: Path,
    editable: bool,
) -> CreativePlan:
    """Read-only: decide what each header / search results image needs.

    Fails before anything is written when an image App Review rejected has not
    been replaced, when a locked version (in a review submission) would need an
    upload or a review submission, or when an image is in a state the release
    cannot resolve. Without images it makes no call and plans nothing.
    """
    sources = creative_sources(creative_dir)
    if not sources:
        return CreativePlan(None, None, [])
    library_id = asset_library_id(client, app_id)
    images = library_images(client, library_id)
    localization_id = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )["id"]
    steps: list[CreativeStep] = []
    for placement_type, label, path in sources:
        image = library_image_for(images, path)
        state = attrs(image).get("state") or ""
        if image is None or state in CREATIVE_PROCESSING_STATES | CREATIVE_READY_STATES:
            if not editable:
                fail(
                    f"cannot send the {label} image {path.name} to App Review while the version is in "
                    "a review submission; re-run once the version can be edited"
                )
            steps.append(CreativeStep(placement_type, label, path, image, "review" if image else "upload"))
            continue
        if state in CREATIVE_PENDING_STATES:
            print(f"  {label}: {path.name} is {state}; it is placed once App Review approves it")
            steps.append(CreativeStep(placement_type, label, path, image, "pending"))
            continue
        if state != "APPROVED":
            fail(f"the {label} image {path.name} is {state or 'in an unknown state'}; resolve it in App Store Connect")
        # Placed when one of the image's placements is this slot on this localization.
        placed = image_placement_ids(client, image["id"])
        slot = slot_placements(client, localization_id, placement_type)
        if any(row["id"] in placed and attrs(row).get("state") != "FAILED" for row in slot):
            print(f"  {label}: {path.name} is already on the {locale} product page")
            steps.append(CreativeStep(placement_type, label, path, image, "placed"))
            continue
        if not editable:
            # Approval can land between a submit and a re-run; the image is
            # placed by a later run, once the version can be edited.
            print(f"  {label}: {path.name} is approved; it is placed once the version can be edited")
            steps.append(CreativeStep(placement_type, label, path, image, "locked"))
            continue
        if len(slot) > 1:
            fail(
                f"the {locale} {label} slot holds {len(slot)} placements "
                f"({', '.join(row['id'] for row in slot)}); leave one in App Store Connect and re-run"
            )
        steps.append(CreativeStep(placement_type, label, path, image, "place", slot[0] if slot else None))
    return CreativePlan(library_id, localization_id, steps)


def place_creative_image(client: AscClient, localization_id: str, step: CreativeStep) -> None:
    """Put an approved image in its slot; replaces the slot's other image in the same request."""
    assert step.image is not None
    group = attrs(step.swap_out).get("placementGroup") or CREATIVE_PLACEMENT_GROUP
    relationships: dict[str, Any] = {
        "image": {"data": {"type": "appAssetLibraryImages", "id": step.image["id"]}},
        "appStoreVersionLocalization": {
            "data": {"type": "appStoreVersionLocalizations", "id": localization_id}
        },
    }
    if step.swap_out is not None:
        relationships["placementToSwapOut"] = {
            "data": {"type": "appAssetLibraryPlacements", "id": step.swap_out["id"]}
        }
    client.post(
        "/appAssetLibraryPlacements",
        {
            "data": {
                "type": "appAssetLibraryPlacements",
                "attributes": {"placementType": step.placement_type, "placementGroup": group},
                "relationships": relationships,
            }
        },
    )
    replaced = f", replacing placement {step.swap_out['id']}" if step.swap_out else ""
    print(f"  {step.label}: placed {step.path.name} on the product page{replaced}")


def apply_creative_plan(
    client: AscClient, plan: CreativePlan, *, app_id: str, reserved_draft: str | None = None
) -> dict[str, str]:
    """Upload, review and place what plan_creative_assets decided. Returns placement type -> outcome."""
    outcomes: dict[str, str] = {}
    to_review: list[tuple[str, dict[str, Any]]] = []
    for step in plan.steps:
        if step.action == "upload":
            assert plan.library_id is not None
            step.image = upload_library_image(client, plan.library_id, step.path)
        elif step.action == "review" and step.image is not None and (
            attrs(step.image).get("state") in CREATIVE_PROCESSING_STATES
        ):
            # An earlier run uploaded it; Apple may still be processing it.
            step.image = wait_for_library_image(client, step.image["id"], step.path.name)
        if step.action in {"upload", "review"}:
            assert step.image is not None
            to_review.append((step.label, step.image))
            outcomes[step.placement_type] = "submitted for review"
    if to_review:
        submit_creative_review(client, app_id, to_review, reserved_draft=reserved_draft)
    for step in plan.steps:
        if step.action == "place":
            assert plan.localization_id is not None
            place_creative_image(client, plan.localization_id, step)
            outcomes[step.placement_type] = "placed"
        elif step.action in {"pending", "placed", "locked"}:
            outcomes[step.placement_type] = step.action
    return outcomes


def submit_creative_review(
    client: AscClient,
    app_id: str,
    images: list[tuple[str, dict[str, Any]]],
    *,
    reserved_draft: str | None = None,
) -> str:
    """Send not-yet-reviewed creative images to App Review in a submission of their own.

    A draft an earlier attempt left holding only some of these images is
    finished first, else an empty draft, and a new one is created only when
    neither exists, so re-runs never pile up drafts. The draft this run keeps
    for the version and its products (reserved_draft) is never taken. Nothing is
    submitted unless the draft then holds exactly these images.
    """
    wanted = {image["id"] for _, image in images}
    partial = empty = None
    for draft in draft_submission_ids(client, app_id):
        if draft == reserved_draft:
            continue
        items = draft_item_relationships(client, draft)
        # An item that is not an image adds None, which is never wanted.
        held = {related_id(item, "appAssetLibraryImage") for item in items}
        if not items:
            empty = empty or draft
        elif held <= wanted:
            partial = draft
            break
    submission_id = partial or empty
    if submission_id:
        print(f"  finishing review submission {submission_id} from an earlier attempt")
    else:
        submission_id = create_draft_submission(client, app_id)
    held = {related_id(item, "appAssetLibraryImage") for item in draft_item_relationships(client, submission_id)}
    for label, image in images:
        if image["id"] in held:
            print(f"  {label}: {attrs(image).get('fileName')} is already in review submission {submission_id}")
            continue
        client.post(
            "/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": submission_id}
                        },
                        "appAssetLibraryImage": {
                            "data": {"type": "appAssetLibraryImages", "id": image["id"]}
                        },
                    },
                }
            },
        )
        print(f"  {label}: {attrs(image).get('fileName')} added to review submission {submission_id}")
    items = draft_item_relationships(client, submission_id)
    held_ids = [related_id(item, "appAssetLibraryImage") for item in items]
    if len(items) != len(wanted) or set(held_ids) != wanted:
        unexpected = [item.get("id") for item in items if related_id(item, "appAssetLibraryImage") not in wanted]
        fail(
            f"review submission {submission_id} does not hold exactly the header and search results "
            f"images (unexpected items: {unexpected or 'none'}); not submitting it"
        )
    submit_product_draft(client, submission_id, what="creative image")
    return submission_id


PRODUCT_GROUPS = ("subscriptions", "non_consumables", "consumables", "tips")


def intended_product_ids(catalog_path: Path = CATALOG) -> set[str]:
    """Product IDs the release is allowed to submit, from the canonical catalog."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for group in PRODUCT_GROUPS:
        for item in catalog.get(group) or []:
            pid = item.get("id")
            if isinstance(pid, str) and pid:
                ids.add(pid)
    return ids


class ProductSubmitError(Exception):
    """A required product could not be added to App Review."""


class ProductSubmitResult:
    """Outcome of attaching products to a review submission."""

    def __init__(
        self,
        submission_id: str,
        submitted: int,
        added_items: bool,
        has_products: bool = False,
    ) -> None:
        self.submission_id = submission_id
        self.submitted = submitted
        self.added_items = added_items
        # True when the draft holds intended products (attached now or in an
        # earlier run), so a catch-up retry can still submit the draft.
        self.has_products = has_products


# States that mean the product can still be (re)attached to a review.
SUBMITTABLE_PRODUCT_STATES = {
    "READY_TO_SUBMIT",
    "DEVELOPER_REJECTED",
    "READY_FOR_REVIEW",
}


def draft_submission_ids(client: AscClient, app_id: str) -> list[str]:
    """The app's READY_FOR_REVIEW (draft, not yet submitted) review submissions."""
    return [
        row["id"]
        for row in iter_collection(
            client,
            f"/apps/{app_id}/reviewSubmissions?"
            + urllib.parse.urlencode({"filter[state]": "READY_FOR_REVIEW", "limit": "5"}),
        )
    ]


def find_draft_submission(
    client: AscClient, app_id: str, version_id: str | None = None
) -> str | None:
    """The draft to use for the App Store version and its products, if there is one.

    The draft already holding version_id wins; otherwise the first draft holding
    no app version, so another version's draft is never touched. A draft holding
    header or search results images belongs to their own review and is skipped,
    so those images never go out with the version.
    """
    drafts = []
    for submission_id in draft_submission_ids(client, app_id):
        items = draft_item_relationships(client, submission_id)
        if any(related_id(item, "appAssetLibraryImage") for item in items):
            continue
        versions = {related_id(item, "appStoreVersion") for item in items} - {None, ""}
        drafts.append((submission_id, versions))
    if version_id:
        for submission_id, versions in drafts:
            if version_id in versions:
                return submission_id
    for submission_id, versions in drafts:
        if not versions:
            return submission_id
    return None


def create_draft_submission(client: AscClient, app_id: str) -> str:
    submission = client.post(
        "/reviewSubmissions",
        {
            "data": {
                "type": "reviewSubmissions",
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    return submission["data"]["id"]


def get_or_create_draft_submission(client: AscClient, app_id: str) -> str:
    """The app's draft review submission for versions and products (see find_draft_submission), creating one."""
    return find_draft_submission(client, app_id) or create_draft_submission(client, app_id)


def version_draft(
    client: AscClient, app_id: str, version_id: str, version_state: str
) -> str | None:
    """Read-only: pick and check the draft the version and its products go out in.

    None means there is none yet (one is created at submit). A READY_FOR_REVIEW
    version already sits in a draft; when no usable draft holds it, this stops
    the run instead of submitting another draft. The preflight runs it before
    the build, and the store step again before its first write.
    """
    draft = find_draft_submission(client, app_id, version_id)
    if version_state == "READY_FOR_REVIEW" and (
        draft is None or version_id not in draft_item_version_ids(client, draft)
    ):
        fail(
            f"App Store version {version_id} is READY_FOR_REVIEW, but no draft review submission "
            "without header or search results images holds it; remove the version from its "
            "submission in App Store Connect and re-run"
        )
    return draft


def reserve_version_draft(
    client: AscClient, app_id: str, version_id: str, version_state: str
) -> str | None:
    """Pick and check the version's draft before anything is written, and reserve it.

    The draft is kept for the version and its products: creative images never take it.
    """
    draft = version_draft(client, app_id, version_id, version_state)
    if draft:
        print(f"  review submission {draft} is kept for the version and its products")
    return draft


def draft_item_relationships(client: AscClient, submission_id: str) -> list[dict[str, Any]]:
    """Return the draft's items with their relationships populated.

    App Store Connect omits `relationships` from Review Submission Items unless
    they are requested with `include`, so every relationship the readers look
    at (versions, products and Asset Library images) is asked for explicitly.
    """
    return iter_collection(
        client,
        f"/reviewSubmissions/{submission_id}/items?limit=50&include={DRAFT_ITEM_INCLUDE}",
    )


def draft_item_version_ids(client: AscClient, submission_id: str) -> set[str]:
    """Return the subscription/in-app purchase/app version ids already attached to a draft."""
    ids: set[str] = set()
    for item in draft_item_relationships(client, submission_id):
        relationships = item.get("relationships") or {}
        for name in ("subscriptionVersion", "appStoreVersion", "inAppPurchaseVersion"):
            related = (relationships.get(name) or {}).get("data") or {}
            if related.get("id"):
                ids.add(related["id"])
    return ids


def draft_holds_app_version(client: AscClient, submission_id: str) -> bool:
    """Return True if any item in the draft references an app store version."""
    for item in draft_item_relationships(client, submission_id):
        related = (
            ((item.get("relationships") or {}).get("appStoreVersion") or {}).get("data")
            or {}
        )
        if related.get("id"):
            return True
    return False


def submit_product_draft(client: AscClient, submission_id: str, *, what: str = "product") -> None:
    """Submit a draft review submission.

    Used on the catch-up path when the app version is already under review and a
    late Ready-to-Submit product was attached to a new draft: the draft is
    submitted on its own so the product actually reaches review. Callers must
    ensure the draft holds only products intended for this release.
    """
    client.request(
        "PATCH",
        f"/reviewSubmissions/{submission_id}",
        body={
            "data": {
                "type": "reviewSubmissions",
                "id": submission_id,
                "attributes": {"submitted": True},
            }
        },
    )
    print(f"  submitted {what} review submission {submission_id}")


def newest_version_id(versions: list[dict[str, Any]]) -> str | None:
    """The highest-numbered product version (App Store Connect allows one in flight)."""
    candidates = [v for v in versions if (v.get("attributes") or {}).get("version")]
    if not candidates:
        return versions[0]["id"] if versions else None
    return max(candidates, key=lambda v: v["attributes"]["version"])["id"]


def current_subscription_version(
    client: AscClient, subscription_id: str
) -> str | None:
    """Return the subscription's current in-flight version id, if any.

    App Store Connect allows only one in-flight version per subscription, so the
    highest-numbered version in a submittable state is the one to attach.
    """
    return newest_version_id(iter_collection(client, f"/subscriptions/{subscription_id}/versions"))


def current_in_app_purchase_version(client: AscClient, iap_id: str) -> str | None:
    """Return the in-app purchase's current in-flight version id, if any (v2 resource)."""
    return newest_version_id(iter_collection(client, f"{ASC_V2_BASE}/inAppPurchases/{iap_id}/versions"))


def submit_in_app_purchases(
    client: AscClient,
    app_id: str,
    *,
    intended_ids: set[str] | None = None,
    submission_id: str | None = None,
) -> ProductSubmitResult:
    """Attach Ready-to-Submit subscriptions / IAPs to the draft review submission.

    As of the 2026 App Store Connect workflow, products are added to the app's
    review submission as Review Submission Items: subscriptions through the
    `subscriptionVersion` relationship (not `subscription`, which the API
    rejects) and one-time purchases (the lifetime unlock) through
    `inAppPurchaseVersion`. Attaching the current in-flight version also clears a
    `DEVELOPER_REJECTED` state left by a prior submission. Nothing reaches App
    Review here: the draft is submitted later in one step, so a failed build
    recheck sends nothing. The deprecated standalone `inAppPurchaseSubmissions`
    endpoint, which submitted a purchase immediately, is not used.

    Only products listed in `store/catalog/products.json` are considered, so an
    unrelated Ready-to-Submit product is not dragged into this release. Products
    that are already approved are left alone.
    """
    if intended_ids is None:
        intended_ids = intended_product_ids()

    if not intended_ids:
        print("  catalog has no products to submit")
        return ProductSubmitResult(submission_id or "", 0, False)

    if submission_id is None:
        submission_id = get_or_create_draft_submission(client, app_id)

    # Idempotency: a retry reuses the same draft, so skip anything already
    # attached instead of posting a duplicate that App Store Connect rejects.
    already_attached = draft_item_version_ids(client, submission_id)

    submitted = 0
    added_items = False
    has_products = False
    failures: list[str] = []

    for group in iter_collection(
        client, f"/apps/{app_id}/subscriptionGroups?limit=200"
    ):
        for sub in iter_collection(
            client, f"/subscriptionGroups/{group['id']}/subscriptions?limit=200"
        ):
            attrs = sub.get("attributes") or {}
            pid = attrs.get("productId") or ""
            if pid not in intended_ids:
                continue
            if attrs.get("state") not in SUBMITTABLE_PRODUCT_STATES:
                continue
            name = attrs.get("name") or sub["id"]
            version_id = current_subscription_version(client, sub["id"])
            if not version_id:
                failures.append(f"subscription {name} ({pid}): no version to submit")
                continue
            if version_id in already_attached:
                submitted += 1
                has_products = True
                print(f"  subscription {name}: already attached (idempotent)")
                continue
            try:
                client.post(
                    "/reviewSubmissionItems",
                    {
                        "data": {
                            "type": "reviewSubmissionItems",
                            "relationships": {
                                "reviewSubmission": {
                                    "data": {
                                        "type": "reviewSubmissions",
                                        "id": submission_id,
                                    }
                                },
                                "subscriptionVersion": {
                                    "data": {
                                        "type": "subscriptionVersions",
                                        "id": version_id,
                                    }
                                },
                            },
                        }
                    },
                )
            except SystemExit:
                failures.append(f"subscription {name} ({pid})")
                continue
            submitted += 1
            added_items = True
            has_products = True
            print(f"  subscription {name}: attached to review submission")

    for iap in iter_collection(client, f"/apps/{app_id}/inAppPurchasesV2?limit=200"):
        attrs = iap.get("attributes") or {}
        pid = attrs.get("productId") or ""
        if pid not in intended_ids:
            continue
        if attrs.get("state") != "READY_TO_SUBMIT":
            continue
        name = attrs.get("name") or iap["id"]
        version_id = current_in_app_purchase_version(client, iap["id"])
        if not version_id:
            failures.append(f"in-app purchase {name} ({pid}): no version to submit")
            continue
        if version_id in already_attached:
            submitted += 1
            has_products = True
            print(f"  in-app purchase {name}: already attached (idempotent)")
            continue
        try:
            client.post(
                "/reviewSubmissionItems",
                {
                    "data": {
                        "type": "reviewSubmissionItems",
                        "relationships": {
                            "reviewSubmission": {
                                "data": {
                                    "type": "reviewSubmissions",
                                    "id": submission_id,
                                }
                            },
                            "inAppPurchaseVersion": {
                                "data": {
                                    "type": "inAppPurchaseVersions",
                                    "id": version_id,
                                }
                            },
                        },
                    }
                },
            )
        except SystemExit:
            failures.append(f"in-app purchase {name} ({pid})")
            continue
        submitted += 1
        added_items = True
        has_products = True
        print(f"  in-app purchase {name}: attached to review submission")

    print(f"  in-app purchases / subscriptions attached: {submitted}")
    if failures:
        raise ProductSubmitError(
            "these required products were not added to App Review: "
            + "; ".join(failures)
            + ". Resolve them in App Store Connect and rerun the release."
        )
    return ProductSubmitResult(submission_id, submitted, added_items, has_products)


def submit_for_review(
    client: AscClient,
    app_id: str,
    version_id: str,
    version_state: str = "",
    *,
    submission_id: str | None = None,
) -> None:
    # Reuse a draft submission when one already exists (an earlier attempt can
    # leave a READY_FOR_REVIEW submission that was never submitted).
    if submission_id is None:
        submission_id = get_or_create_draft_submission(client, app_id)

    items_versions = draft_item_version_ids(client, submission_id)
    # READY_FOR_REVIEW means the version is already held by this draft submission.
    already_added = version_state == "READY_FOR_REVIEW" or version_id in items_versions
    if not already_added:
        client.post(
            "/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": submission_id}
                        },
                        "appStoreVersion": {
                            "data": {"type": "appStoreVersions", "id": version_id}
                        },
                    },
                }
            },
        )

    # Submitting is a PATCH with submitted=true; there is no /submit action.
    client.request(
        "PATCH",
        f"/reviewSubmissions/{submission_id}",
        body={
            "data": {
                "type": "reviewSubmissions",
                "id": submission_id,
                "attributes": {"submitted": True},
            }
        },
    )
    print(
        f"  submitted App Store version {version_id} for review "
        f"(submission {submission_id})"
    )


RELEASE_TYPE_PUBLIC = "AFTER_APPROVAL"
RELEASE_TYPE_MANUAL = "MANUAL"


def verify_build(
    client: AscClient, build_id: str, version_string: str, build_number: str
) -> None:
    """Re-read the build and refuse anything but this run's processed build."""
    payload = client.get(
        f"/builds/{build_id}?"
        + urllib.parse.urlencode(
            {
                "include": "preReleaseVersion",
                "fields[builds]": "version,processingState,expired,preReleaseVersion",
                "fields[preReleaseVersions]": "version,platform",
            }
        )
    )
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    pre = next(
        (row for row in payload.get("included") or [] if row.get("type") == "preReleaseVersions"),
        {},
    )
    pre_attrs = pre.get("attributes") or {}
    problems = []
    if data.get("id") != build_id:
        problems.append(f"id {data.get('id')!r}")
    if attrs.get("version") != build_number:
        problems.append(f"build number {attrs.get('version')!r}")
    if attrs.get("processingState") != "VALID":
        problems.append(f"processing state {attrs.get('processingState')!r}")
    if attrs.get("expired"):
        problems.append("expired")
    if pre_attrs.get("version") != version_string or pre_attrs.get("platform") != "IOS":
        problems.append(f"version {pre_attrs.get('platform')} {pre_attrs.get('version')!r}")
    if problems:
        fail(
            f"build {build_id} is not {version_string} ({build_number}), VALID and unexpired: "
            + ", ".join(problems)
        )


def attached_build_id(client: AscClient, version_id: str) -> str | None:
    payload = client.get(f"/appStoreVersions/{version_id}/build")
    return (payload.get("data") or {}).get("id")


def confirm_selected_build(client: AscClient, version_id: str, build_id: str) -> None:
    current = attached_build_id(client, version_id)
    if current != build_id:
        fail(
            f"App Store version {version_id} has build {current or 'none'} selected, "
            f"expected {build_id}; not submitting"
        )


def select_build(
    client: AscClient,
    version_id: str,
    build_id: str,
    version_string: str,
    build_number: str,
) -> None:
    """Explicitly select this run's build for the version, then read it back."""
    verify_build(client, build_id, version_string, build_number)
    client.request(
        "PATCH",
        f"/appStoreVersions/{version_id}/relationships/build",
        body={"data": {"type": "builds", "id": build_id}},
    )
    confirm_selected_build(client, version_id, build_id)
    print(f"  selected build {build_number} ({build_id}) for version {version_string}")


def set_release_type(client: AscClient, version_id: str, *, public_release: bool) -> None:
    release_type = RELEASE_TYPE_PUBLIC if public_release else RELEASE_TYPE_MANUAL
    client.patch(
        f"/appStoreVersions/{version_id}",
        {
            "data": {
                "type": "appStoreVersions",
                "id": version_id,
                "attributes": {"releaseType": release_type},
            }
        },
    )
    print(
        "  release type: "
        + ("release automatically after approval" if public_release else "manual release after approval")
    )


class _RecordingClient:
    """Offline AscClient stand-in for the dry-run self-check."""

    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self._collections = collections
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, path: str) -> dict[str, Any]:
        return {"data": self._collections.get(path, [])}

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, body))
        return {"data": {"id": "fake"}}

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError(f"unexpected PATCH {path}")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.requests.append((method, path, body))
        return {"data": {"id": "fake"}}


ITEMS_PATH = f"/reviewSubmissions/SUB1/items?limit=50&include={DRAFT_ITEM_INCLUDE}"


def _attached_ids(posts: list[tuple[str, dict[str, Any]]], relationship: str) -> set[str]:
    """Version ids attached to a review submission through one item relationship."""
    return {
        body["data"]["relationships"][relationship]["data"]["id"]
        for path, body in posts
        if path == "/reviewSubmissionItems" and relationship in body["data"]["relationships"]
    }


def dry_run_self_check() -> None:
    """Exercise the product-submission path offline so dry-run covers it."""
    app_id = "APP"
    groups_path = f"/apps/{app_id}/subscriptionGroups?limit=200"
    subs_path = "/subscriptionGroups/G1/subscriptions?limit=200"
    submission_path = (
        f"/apps/{app_id}/reviewSubmissions?"
        + urllib.parse.urlencode({"filter[state]": "READY_FOR_REVIEW", "limit": "5"})
    )
    weekly = "com.quitall.premium.weekly"
    yearly = "com.quitall.premium.yearly"
    lifetime = "com.quitall.premium.lifetime"
    collections: dict[str, list[dict[str, Any]]] = {
        groups_path: [{"id": "G1"}],
        subs_path: [
            # Weekly is still DEVELOPER_REJECTED from a prior submit.
            {"id": "S0", "attributes": {"name": "Weekly", "productId": weekly, "state": "DEVELOPER_REJECTED"}},
            {"id": "S1", "attributes": {"name": "Yearly", "productId": yearly, "state": "READY_TO_SUBMIT"}},
            # Not in the catalog: must never be submitted.
            {"id": "SX", "attributes": {"name": "Legacy", "productId": "com.quitall.premium.monthly",
                                        "state": "READY_TO_SUBMIT"}},
        ],
        submission_path: [{"id": "SUB1"}],
        f"/apps/{app_id}/inAppPurchasesV2?limit=200": [
            {"id": "IAP1", "attributes": {"name": "Lifetime", "productId": lifetime, "state": "READY_TO_SUBMIT"}},
            # Already approved: must not be resubmitted.
            {"id": "IAP2", "attributes": {"name": "Old lifetime", "productId": "com.quitall.premium.lifetime.v0",
                                          "state": "APPROVED"}},
        ],
    }
    for i in range(2):
        # Two versions returned out of order: the higher-numbered current
        # version must win regardless of response ordering.
        collections[f"/subscriptions/S{i}/versions"] = [
            {"id": f"V{i}b", "attributes": {"version": 2, "state": "READY_FOR_REVIEW"}},
            {"id": f"V{i}", "attributes": {"version": 1, "state": "DEVELOPER_REJECTED"}},
        ]
    # The lifetime purchase's approved version 1 and its new version 2, out of order.
    collections[f"{ASC_V2_BASE}/inAppPurchases/IAP1/versions"] = [
        {"id": "IV1", "attributes": {"version": 1, "state": "APPROVED"}},
        {"id": "IV2", "attributes": {"version": 2, "state": "PREPARE_FOR_SUBMISSION"}},
    ]
    intended = {weekly, yearly, lifetime, "com.quitall.premium.lifetime.v0"}
    fake = _RecordingClient(collections)
    result = submit_in_app_purchases(fake, app_id, intended_ids=intended)
    assert result.submitted == 3, f"expected 3 products, got {result.submitted}"
    assert result.added_items, "expected added_items=True"
    assert result.has_products, "expected has_products=True"
    assert result.submission_id == "SUB1", f"wrong submission: {result.submission_id}"
    item_posts = [body for path, body in fake.posts if path == "/reviewSubmissionItems"]
    attached_versions = _attached_ids(fake.posts, "subscriptionVersion")
    assert attached_versions == {"V0b", "V1b"}, (
        f"expected the current (v2) version of each subscription, got {attached_versions}"
    )
    attached_purchases = _attached_ids(fake.posts, "inAppPurchaseVersion")
    assert attached_purchases == {"IV2"}, (
        f"only the Ready-to-Submit lifetime purchase's current version may be attached, got {attached_purchases}"
    )
    assert all(
        body["data"]["relationships"]["reviewSubmission"]["data"]["id"] == "SUB1"
        for body in item_posts
    ), "wrong review submission target"
    assert not [path for path, _ in fake.posts if path == "/inAppPurchaseSubmissions"], (
        "the deprecated immediate in-app purchase submission must not be used"
    )
    assert not fake.requests, "attaching products must not submit anything"

    # Retry: the same draft already holds a subscription version and the lifetime
    # purchase's version, so the rerun must not post them again (App Store
    # Connect rejects duplicates).
    collections[ITEMS_PATH] = [
        {
            "id": "ITEM0",
            "relationships": {
                "subscriptionVersion": {"data": {"type": "subscriptionVersions", "id": "V0b"}},
                "appStoreVersion": {"data": None},
            },
        },
        {
            "id": "ITEM1",
            "relationships": {
                "inAppPurchaseVersion": {"data": {"type": "inAppPurchaseVersions", "id": "IV2"}},
                "appStoreVersion": {"data": None},
            },
        },
    ]
    retry = _RecordingClient(collections)
    result2 = submit_in_app_purchases(retry, app_id, intended_ids=intended)
    assert result2.submitted == 3, f"expected 3 products on retry, got {result2.submitted}"
    assert result2.has_products, "retry must report the draft holds products"
    retry_versions = _attached_ids(retry.posts, "subscriptionVersion") | _attached_ids(
        retry.posts, "inAppPurchaseVersion"
    )
    assert retry_versions == {"V1b"}, f"retry attached the wrong versions: {retry_versions}"
    assert draft_holds_app_version(retry, "SUB1") is False, "false app-version detection"

    collections[ITEMS_PATH] = [
        {
            "id": "ITEMV",
            "relationships": {
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": "AV1"}},
                "subscriptionVersion": {"data": None},
            },
        }
    ]
    assert draft_holds_app_version(_RecordingClient(collections), "SUB1") is True, (
        "app-version items must be detected and refused"
    )

    # submit_for_review must not re-add an app version already in the draft.
    review = _RecordingClient(collections)
    submit_for_review(review, app_id, "AV1", "PREPARE_FOR_SUBMISSION", submission_id="SUB1")
    assert not [path for path, _ in review.posts if path == "/reviewSubmissionItems"], (
        "app version was posted again on retry"
    )
    assert any(
        method == "PATCH"
        and path == "/reviewSubmissions/SUB1"
        and body is not None
        and body["data"]["attributes"]["submitted"] is True
        for method, path, body in review.requests
    ), "submission was not PATCHed to submitted=true"
    print("  dry-run self-check: product submission selection OK")


def validate_local_inputs(
    *,
    upload_listing: bool,
    upload_whats_new: bool,
    upload_screenshots: bool,
    metadata_dir: Path,
    locales: list[str],
    version: str | None,
    root: Path | None = None,
    upload_creative: bool = False,
    creative_dir: Path | None = None,
) -> dict[str, dict[str, list[Path]]]:
    """Check every repo input before any App Store Connect call.

    Returns the committed screenshot sets per locale (empty unless screenshots
    are on); stage_screenshots makes the copies that are uploaded.
    """
    if (upload_listing or upload_whats_new or upload_screenshots) and not locales:
        fail(f"no listing locales under {metadata_dir / 'ios'}")
    problems: list[str] = []
    for locale in locales:
        loc_dir = metadata_dir / "ios" / locale
        if not loc_dir.is_dir():
            fail(f"metadata dir missing: {loc_dir}")
        if upload_listing:
            problems += check_listing_text(loc_dir, LISTING_FIELDS)
        if upload_whats_new:
            problems += check_listing_text(loc_dir, (WHATS_NEW_FIELD,))
    if upload_whats_new:
        # What's New is written for one version; stale text must never ship.
        marker_path = metadata_dir / "ios" / WHATS_NEW_VERSION_FILE
        marker = read_text(marker_path)
        if not version:
            problems.append("What's New upload needs --version to check the text is for this release")
        elif marker != version:
            problems.append(
                f"What's New upload is on for {version}, but {marker_path} says the text is for "
                f"{marker or 'no version'}; update whats_new.txt in every locale, then set "
                f"{WHATS_NEW_VERSION_FILE} to {version}"
            )
    if problems:
        fail("listing files are not ready:\n  " + "\n  ".join(problems))

    sets: dict[str, dict[str, list[Path]]] = {}
    if upload_screenshots:
        for locale in locales:
            if (metadata_dir / "ios" / locale / SCREENSHOT_MANIFEST).is_file():
                sets[locale] = load_screenshot_sets(metadata_dir, locale, root)
        if not sets:
            fail(
                f"screenshot upload is on, but no locale under {metadata_dir / 'ios'} has a "
                f"{SCREENSHOT_MANIFEST}; add App Store-sized PNGs and list them there"
            )
        if not any(display_type in WATCH_LABELS for locale_sets in sets.values() for display_type in locale_sets):
            print("  no Apple Watch screenshots listed; the Watch sets in App Store Connect are left alone")
    if upload_creative:
        creative_sources(creative_dir or DEFAULT_CREATIVE_DIR)
    return sets


def version_key(version_string: str) -> tuple[int, ...] | None:
    """1.2.2 -> (1, 2, 2), padded to three parts; None when it is not numeric."""
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){0,2}", version_string or ""):
        return None
    parts = [int(part) for part in version_string.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def ensure_ios_app_store_version(
    client: AscClient, *, bundle_id: str, expected_app_id: str, version_string: str
) -> None:
    """Create the App Store version for this release when it does not exist yet.

    An existing version with this number is left as it is (the preflight then
    checks its state). Nothing is created when another version is still on its
    way to the store, or when this number is not newer than every existing
    version; the created version is read back before the run continues.
    """
    wanted = version_key(version_string)
    if wanted is None:
        fail(f"version {version_string!r} must look like 1.3 or 1.2.2")
    app_id = find_app(client, bundle_id)
    if app_id != expected_app_id:
        fail(f"{bundle_id} resolved to App Store Connect app {app_id}, expected {expected_app_id}")
    versions = list_ios_app_store_versions(client, app_id)
    existing = [
        row for row in versions if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if len(existing) > 1:
        fail(f"ambiguous App Store versions for {version_string!r} on IOS — got {len(existing)} records")
    if existing:
        state = (existing[0].get("attributes") or {}).get("appStoreState") or ""
        print(f"App Store version {version_string} already exists ({state}); nothing to create")
        return
    problems: list[str] = []
    for row in versions:
        attrs = row.get("attributes") or {}
        other = attrs.get("versionString") or ""
        state = attrs.get("appStoreState") or ""
        if state not in FINISHED_VERSION_STATES:
            problems.append(
                f"version {other!r} is still {state or 'in an unknown state'}; release or remove it "
                f"in App Store Connect before {version_string} can be created"
            )
        other_key = version_key(other)
        if other_key is not None and other_key >= wanted:
            problems.append(f"version {version_string} is not newer than the existing version {other}")
    if problems:
        fail(
            f"not creating App Store version {version_string} (nothing was changed):\n  "
            + "\n  ".join(problems)
        )
    client.post(
        "/appStoreVersions",
        {
            "data": {
                "type": "appStoreVersions",
                "attributes": {"platform": "IOS", "versionString": version_string},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    created = [
        row
        for row in list_ios_app_store_versions(client, app_id)
        if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if len(created) != 1:
        fail(
            f"created App Store version {version_string}, but reading it back found "
            f"{len(created)} records; check it in App Store Connect before rerunning"
        )
    state = (created[0].get("attributes") or {}).get("appStoreState") or ""
    print(f"created App Store version {version_string} ({state or 'state not reported yet'})")


def version_state_problems(state: str, version: str, *, edits_metadata: bool, submit: bool) -> list[str]:
    """Version states the enabled switches cannot use at all, checked before anything else."""
    problems: list[str] = []
    metadata_states = TEXT_EDITABLE_VERSION_STATES | UNCHANGED_METADATA_STATES
    if edits_metadata and state not in metadata_states:
        problems.append(
            f"version {version!r} is not editable for metadata (state={state}); "
            f"expected one of: {', '.join(sorted(metadata_states))}"
        )
    # WAITING_FOR_REVIEW is accepted only as a rerun after a completed submit,
    # and only when review holds this run's build (checked by the caller).
    if submit and state not in SUBMITTABLE_VERSION_STATES | {"WAITING_FOR_REVIEW"}:
        problems.append(f"version {version!r} cannot be submitted for review (state={state})")
    return problems


def preflight(
    client: AscClient,
    *,
    bundle_id: str,
    expected_app_id: str,
    version_string: str,
    locales: list[str],
    metadata_dir: Path,
    do_listing: bool,
    do_whats_new: bool,
    do_screenshots: bool,
    screenshot_sets: dict[str, dict[str, list[Path]]],
    do_submit: bool,
    do_creative: bool = False,
    creative_dir: Path | None = None,
    copies: dict[Path, Path] | None = None,
) -> None:
    """Read-only App Store Connect checks before anything is built or uploaded.

    GET requests only: the App Store version must exist (IOS_CREATE_VERSION's
    step, which runs before this, creates a missing one), be in a state the enabled switches can use and have every
    repository locale, and its metadata must be changeable where it differs.
    With review submission on, a READY_FOR_REVIEW version's draft is checked
    too. With creative assets on, their Asset Library state is checked the way
    the store step plans it, so an image the version cannot take now (rejected,
    or needing upload or review while the version sits in a review submission)
    stops the run before the build instead of after it.
    The live step repeats every check, because App Store Connect can change
    while the build is processed.
    """
    app_id = find_app(client, bundle_id)
    if app_id != expected_app_id:
        fail(f"{bundle_id} resolved to App Store Connect app {app_id}, expected {expected_app_id}")
    version = get_ios_app_store_version(client, app_id, version_string)
    state = (version.get("attributes") or {}).get("appStoreState") or ""
    edits_metadata = do_listing or do_whats_new or do_screenshots
    problems = version_state_problems(
        state, version_string, edits_metadata=edits_metadata or do_creative, submit=do_submit
    )
    if do_submit and state == "WAITING_FOR_REVIEW":
        problems.append(
            f"version {version_string!r} is already WAITING_FOR_REVIEW, so the build this run would "
            "upload could never be selected; take the version out of review in App Store Connect first"
        )
    if not problems and edits_metadata:
        plans = plan_metadata(
            client,
            version_id=version["id"],
            locales=locales,
            metadata_dir=metadata_dir,
            include_listing=do_listing,
            include_whats_new=do_whats_new,
            screenshot_sets=screenshot_sets,
            copies=copies,
        )
        problems += metadata_problems(plans, version=version_string, state=state)
        for plan in plans:
            changes = list(plan.text_changes) + plan.stale_screenshots
            print(f"  {plan.locale}: {'will update ' + ', '.join(changes) if changes else 'already up to date'}")
    if problems:
        fail(
            "App Store Connect is not ready for this release (nothing was built or changed):\n  "
            + "\n  ".join(problems)
        )
    if do_submit and state == "READY_FOR_REVIEW":
        # The store step submits the draft holding this version; finding out
        # after the build and upload that there is none would be too late.
        version_draft(client, app_id, version["id"], state)
    if do_creative:
        plan_creative_assets(
            client,
            app_id=app_id,
            version_id=version["id"],
            locale=CREATIVE_LOCALE,
            creative_dir=creative_dir or DEFAULT_CREATIVE_DIR,
            editable=state in EDITABLE_VERSION_STATES,
        )
    print(f"ASC preflight passed: version {version_string} is {state}; nothing was changed")


def run(argv: list[str] | None, staging_dir: Path) -> None:
    """One run of the step; staging_dir is the temporary folder the screenshot copies go to."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=METADATA_DIR,
        help="Listing tree: <dir>/ios/<locale>/*.txt and screenshots.json",
    )
    parser.add_argument(
        "--creative-dir",
        type=Path,
        default=DEFAULT_CREATIVE_DIR,
        help="Product page header and search results images, one folder per slot: "
        + ", ".join(folder for folder, _, _, _ in CREATIVE_SLOTS),
    )
    parser.add_argument(
        "--locale",
        action="append",
        help="Only these locales (repeatable). Default: every locale directory in the repo",
    )
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--app-id", default=DEFAULT_APP_ID, help="Expected App Store Connect app id")
    parser.add_argument(
        "--version",
        help="Marketing version from release tag (e.g. 1.2.2 when tag is v1.2.2)",
    )
    parser.add_argument(
        "--build-id",
        help="App Store Connect id of the build uploaded by this run (required to submit)",
    )
    parser.add_argument(
        "--build-number",
        help="CFBundleVersion of the build uploaded by this run (required to submit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate flags and local files only — no ASC HTTP calls",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Read-only App Store Connect checks before the build is uploaded (GET requests only)",
    )
    parser.add_argument(
        "--create-version",
        action="store_true",
        help="Create the App Store version for --version when it is missing (CREATE_IOS_VERSION)",
    )
    args = parser.parse_args(argv)

    if args.create_version:
        if args.preflight:
            fail("--create-version and --preflight are separate steps; run them one at a time")
        if not truthy("CREATE_IOS_VERSION"):
            print("App Store version creation skipped — CREATE_IOS_VERSION is off")
            return
        if not args.version:
            fail("--create-version needs --version (the marketing version from the tag)")
        if version_key(args.version) is None:
            fail(f"version {args.version!r} must look like 1.3 or 1.2.2")
        if args.dry_run:
            print(f"ASC dry-run — would create App Store version {args.version} if it is missing")
            return
        ensure_ios_app_store_version(
            client_from_environment(),
            bundle_id=args.bundle_id,
            expected_app_id=args.app_id,
            version_string=args.version,
        )
        return

    do_listing = truthy("UPLOAD_LISTING")
    do_whats_new = truthy("UPLOAD_WHATS_NEW")
    do_screenshots = truthy("UPLOAD_SCREENSHOTS")
    do_creative = truthy("UPLOAD_CREATIVE_ASSETS")
    do_submit = truthy("SUBMIT_IOS_REVIEW")
    do_public = truthy("PUBLIC_RELEASE")

    if not (do_listing or do_whats_new or do_screenshots or do_creative or do_submit):
        print(
            "ASC release skipped — UPLOAD_LISTING / UPLOAD_WHATS_NEW / UPLOAD_SCREENSHOTS / "
            "UPLOAD_CREATIVE_ASSETS / SUBMIT_IOS_REVIEW are off"
        )
        return
    if do_public and not do_submit:
        fail("PUBLIC_RELEASE only applies when SUBMIT_IOS_REVIEW is on")
    if do_submit and not args.preflight and not (args.build_id and args.build_number):
        fail(
            "submitting needs --build-id and --build-number for the build uploaded by "
            "this run; an older build is never substituted"
        )

    locales = args.locale or listing_locales(args.metadata_dir)
    screenshot_sets = validate_local_inputs(
        upload_listing=do_listing,
        upload_whats_new=do_whats_new,
        upload_screenshots=do_screenshots,
        metadata_dir=args.metadata_dir,
        locales=locales,
        version=args.version,
        upload_creative=do_creative,
        creative_dir=args.creative_dir,
    )
    # The RGB copies that are uploaded, checked before any App Store Connect call.
    copies = stage_screenshots(screenshot_sets, staging_dir)

    if args.dry_run:
        print("ASC dry-run — local inputs OK; no API calls")
        print(
            f"  bundle={args.bundle_id} locales={','.join(locales)} listing={do_listing} "
            f"whats_new={do_whats_new} screenshots={do_screenshots} creative={do_creative} "
            f"submit={do_submit} public_release={do_public}"
        )
        for locale, sets in screenshot_sets.items():
            for display_type, pngs in sets.items():
                print(f"  {locale} {display_type}: {len(pngs)} screenshot(s)")
        if do_submit:
            dry_run_self_check()
        return

    if not args.version:
        fail("--version is required for live ASC calls (pass marketing version from tag)")

    client = client_from_environment()
    edits_metadata = do_listing or do_whats_new or do_screenshots
    if args.preflight:
        preflight(
            client,
            bundle_id=args.bundle_id,
            expected_app_id=args.app_id,
            version_string=args.version,
            locales=locales,
            metadata_dir=args.metadata_dir,
            do_listing=do_listing,
            do_whats_new=do_whats_new,
            do_screenshots=do_screenshots,
            screenshot_sets=screenshot_sets,
            do_submit=do_submit,
            do_creative=do_creative,
            creative_dir=args.creative_dir,
            copies=copies,
        )
        return

    app_id = find_app(client, args.bundle_id)
    if app_id != args.app_id:
        fail(f"{args.bundle_id} resolved to App Store Connect app {app_id}, expected {args.app_id}")
    version = get_ios_app_store_version(client, app_id, args.version)
    version_id = version["id"]
    state = (version.get("attributes") or {}).get("appStoreState") or ""
    print(
        f"ASC app={app_id} version={args.version} id={version_id} state={state}"
    )

    problems = version_state_problems(
        state, args.version, edits_metadata=edits_metadata or do_creative, submit=do_submit
    )
    if problems:
        fail("; ".join(problems))
    in_review = state == "WAITING_FOR_REVIEW"
    if in_review and do_submit:
        # A rerun after a successful submit: only accept it if review holds
        # exactly this run's build.
        confirm_selected_build(client, version_id, args.build_id)
    # Pick and check the version's draft before any write. It is reserved for
    # the version and its products, so header and search results images never
    # go out in it.
    reserved_draft: str | None = None
    if do_submit and not in_review:
        reserved_draft = reserve_version_draft(client, app_id, version_id, state)

    plans: list[LocalePlan] = []
    if edits_metadata:
        # Read everything first, so a change the version's state cannot take
        # fails before anything is written. Only what differs is written, which
        # also makes a rerun after a started or completed submission work.
        plans = plan_metadata(
            client,
            version_id=version_id,
            locales=locales,
            metadata_dir=args.metadata_dir,
            include_listing=do_listing,
            include_whats_new=do_whats_new,
            screenshot_sets=screenshot_sets,
            copies=copies,
        )
        problems = metadata_problems(plans, version=args.version, state=state)
        if problems:
            fail(
                "cannot update the version metadata (nothing was changed):\n  "
                + "\n  ".join(problems)
                + "\n  To change it, take the version out of review in App Store Connect, then rerun."
            )
    # Header and search results images are planned read-only too, before the first write.
    creative_plan: CreativePlan | None = None
    if do_creative:
        print("checking ASC product page header and search results images…")
        creative_plan = plan_creative_assets(
            client,
            app_id=app_id,
            version_id=version_id,
            locale=CREATIVE_LOCALE,
            creative_dir=args.creative_dir,
            editable=state in EDITABLE_VERSION_STATES,
        )
    if edits_metadata:
        print("updating App Store metadata (only what differs)…")
        apply_metadata(client, plans)
    if creative_plan is not None and creative_plan.steps:
        print("syncing ASC product page header and search results images…")
        apply_creative_plan(client, creative_plan, app_id=app_id, reserved_draft=reserved_draft)

    if in_review:
        if do_submit:
            print(
                "ASC submit skipped — version already WAITING_FOR_REVIEW with this run's "
                "build (idempotent)"
            )
            try:
                result = submit_in_app_purchases(client, app_id)
            except ProductSubmitError as exc:
                fail(str(exc))
            # Attaching a product to a fresh draft does not submit it. If the
            # version is already under review, the draft holding the late
            # products must be submitted on its own or they never reach review.
            # `has_products` also covers a retry where an earlier run attached
            # the products but failed to submit their draft. Only submit a
            # product-only draft: refuse if it holds an app version (e.g.
            # another release's) so unrelated items never ship.
            if result.has_products and result.submission_id:
                if draft_holds_app_version(client, result.submission_id):
                    fail(
                        "refusing to submit a draft that contains an app "
                        "version; remove it in App Store Connect and rerun"
                    )
                submit_product_draft(client, result.submission_id)
        print("ASC release step finished")
        return

    if do_submit:
        print("selecting this run's build…")
        select_build(client, version_id, args.build_id, args.version, args.build_number)
        set_release_type(client, version_id, public_release=do_public)
        print("attaching in-app purchases / subscriptions to the review submission…")
        submission_id = reserved_draft or create_draft_submission(client, app_id)
        try:
            result = submit_in_app_purchases(
                client, app_id, submission_id=submission_id
            )
        except ProductSubmitError as exc:
            # A first-time subscription must ship with the version, so a failed
            # required product blocks the whole submission rather than letting
            # the version enter review without it.
            fail(f"{exc} Not submitting the app version.")
        # Recheck immediately before submitting: the selection must still be
        # this run's processed build. Nothing has reached App Review yet; the
        # version and its products go out together with the one submit below.
        verify_build(client, args.build_id, args.version, args.build_number)
        confirm_selected_build(client, version_id, args.build_id)
        print("submitting for App Store review…")
        submit_for_review(
            client, app_id, version_id, state, submission_id=submission_id
        )
        print(f"  {result.submitted} product(s) went out with the version")

    print("ASC release step finished")


def main(argv: list[str] | None = None) -> None:
    # Screenshots are uploaded from RGB copies in this temporary folder, which is
    # removed when the step ends; the committed files are never changed.
    with tempfile.TemporaryDirectory(prefix="asc-screenshots-") as staging:
        run(argv, Path(staging))


if __name__ == "__main__":
    main()
