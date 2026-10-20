#!/usr/bin/env python3
"""ABYSM App Store Connect version, listing, screenshots, creative assets, build selection and review (gated in CI).

Called by .github/workflows/ios-release.yml with flags resolved from the IOS_*
switches (see store/README.md):

  UPLOAD_LISTING      version text (description, keywords, promotional text,
                      marketing/support URLs) and App Info (name, subtitle,
                      privacy policy URL) for every locale in store/metadata/ios/
  UPLOAD_WHATS_NEW    whats_new.txt for every locale
  UPLOAD_SCREENSHOTS  iPhone 6.9" and iPad 13" screenshots from store/metadata/screenshots/,
                      plus iPhone Duo and Apple Watch sets when their folders hold
                      PNGs, uploaded as RGB copies (no alpha channel)
  UPLOAD_CREATIVE_ASSETS
                      product page header and search results images from
                      store/creative/ios/
  SUBMIT_IOS_REVIEW   select THIS run's build (read back + rechecked), then submit the
                      version and any Ready-to-Submit catalog products for review
  PUBLIC_RELEASE      release automatically after approval (else manual release)
  CREATE_IOS_VERSION  with --create-version: create the tag's App Store version
                      when it is missing (an existing one is left alone)

Only fields, screenshots and selections that differ from App Store Connect are
sent, so re-running the step is safe. A screenshot set counts as matching only
when it holds the repo's images in order and App Store Connect has processed
every one, so a set left by a failed or timed-out wait is uploaded again. Once
the version is in a draft review submission (READY_FOR_REVIEW) its text can
still change but its screenshots cannot. Once it is submitted (WAITING_FOR_REVIEW,
IN_REVIEW) nothing can, and a re-run writes nothing: it succeeds only if App
Store Connect already matches the repo, review holds this run's build, and no
catalog product is still due for review (a product is never sent to App Review
without the version).

App Store Connect refuses a screenshot that stores an alpha channel, even when
every pixel is opaque, so the committed screenshots are only read: the run
uploads RGB copies made in a temporary folder (transparent pixels laid over
black, colour profile kept), and refuses any copy that still stores alpha.

Product page header and search results images (UPLOAD_CREATIVE_ASSETS) live in
the app's Asset Library. A new image is uploaded once, sent to App Review in a
review submission of its own (never the version's draft), and placed on the
version's localization once Apple approves it.

`--dry-run` validates the local inputs only and makes no network calls.
`--create-version` (CREATE_IOS_VERSION, from the IOS_CREATE_VERSION switch) runs
first on a release run: it adds the App Store version for the tag when App Store
Connect does not have it yet, and leaves an existing one alone. It refuses when
another version is still on its way to the store or the tag's version is not
newer than every existing one.
`--preflight` runs before anything is built: GET requests only, it fails when the
App Store version is missing (turn on IOS_CREATE_VERSION, or create it in App
Store Connect first) or when its state, localizations, draft review submission
(a READY_FOR_REVIEW version no usable draft holds), header and search results
images (one the version cannot take now) or catalog products would stop the
enabled steps.
"""
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
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]
ASC_ROOT = "https://api.appstoreconnect.apple.com"
ASC_BASE = f"{ASC_ROOT}/v1"
DEFAULT_BUNDLE_ID = "com.apoorvdarshan.Abysm"
# App Store Connect app record for the bundle id ("ABYSM - Dive the Deep Sea").
APP_STORE_APP_ID = "6783505799"
METADATA_DIR = ROOT / "store" / "metadata"
CATALOG = ROOT / "store" / "catalog" / "products.json"
PRIMARY_LOCALE = "en-US"

# Everything about the version can change: text, App Info and screenshots.
EDITABLE_VERSION_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "REJECTED",
    "METADATA_REJECTED",
    "INVALID_BINARY",
}
# READY_FOR_REVIEW means the version already sits in a draft review submission
# (for example after a failed submit). It can still be submitted, and its text
# (listing, What's New, App Info) can still change; its screenshots cannot.
TEXT_EDITABLE_VERSION_STATES = EDITABLE_VERSION_STATES | {"READY_FOR_REVIEW"}
SUBMITTABLE_VERSION_STATES = EDITABLE_VERSION_STATES | {"READY_FOR_REVIEW"}
# Already submitted: nothing can change, and a re-run of the store step (for
# example after the job failed once the submit had gone through) writes nothing.
# It passes only when App Store Connect already matches the repo, review holds
# this run's build, and no catalog product is left Ready to Submit.
SUBMITTED_VERSION_STATES = {"WAITING_FOR_REVIEW", "IN_REVIEW"}
# App Info (name, subtitle, privacy URL) is editable only while a new version is
# prepared, including while that version waits in a draft review submission.
EDITABLE_APP_INFO_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "REJECTED",
    "METADATA_REJECTED",
    "INVALID_BINARY",
    "READY_FOR_REVIEW",
}
# App Info records of a version that is already on the App Store (never edited).
LIVE_APP_INFO_STATES = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION", "REPLACED_WITH_NEW_INFO"}
# Versions in these states are finished. Any other state means the version is
# still on its way to the store, and App Store Connect allows only one such
# version per platform, so --create-version never creates a version next to it.
FINISHED_VERSION_STATES = {
    "READY_FOR_SALE",
    "REPLACED_WITH_NEW_VERSION",
    "REMOVED_FROM_SALE",
    "DEVELOPER_REMOVED_FROM_SALE",
}

# (ASC attribute, file in store/metadata/ios/<locale>/, max characters)
LISTING_FIELDS = (
    ("description", "description.txt", 4000),
    ("keywords", "keywords.txt", 100),
    ("promotionalText", "promotional_text.txt", 170),
    ("marketingUrl", "marketing_url.txt", None),
    ("supportUrl", "support_url.txt", None),
)
WHATS_NEW_FIELD = ("whatsNew", "whats_new.txt", 4000)
APP_INFO_FIELDS = (
    ("name", "name.txt", 30),
    ("subtitle", "subtitle.txt", 30),
    ("privacyPolicyUrl", "privacy_url.txt", None),
    ("privacyChoicesUrl", "privacy_choices_url.txt", None),
)
URL_ATTRIBUTES = {"marketingUrl", "supportUrl", "privacyPolicyUrl", "privacyChoicesUrl"}

# Screenshots come straight from the repo's App Store-ready images.
# Display type → (source folder, accepted pixel sizes). ABYSM is landscape-only.
SCREENSHOT_SETS: tuple[tuple[str, Path, frozenset[tuple[int, int]]], ...] = (
    (
        "APP_IPHONE_67",  # App Store Connect's 6.9"/6.7" iPhone slot
        ROOT / "store" / "metadata" / "screenshots" / "ios" / "iphone",
        frozenset({(2796, 1290), (1290, 2796)}),
    ),
    (
        "APP_IPAD_PRO_3GEN_129",  # 13"/12.9" iPad slot
        ROOT / "store" / "metadata" / "screenshots" / "ios" / "ipad",
        frozenset({(2752, 2064), (2064, 2752), (2732, 2048), (2048, 2732)}),
    ),
)
# Optional sets, uploaded with the others when their folder holds PNGs; without
# any, the live set in App Store Connect is left alone. ABYSM has none yet. One
# iPhone Duo set takes both screens: outer 1398x2034 and inner 2007x2853.
OPTIONAL_SCREENSHOT_SETS: tuple[tuple[str, Path, frozenset[tuple[int, int]]], ...] = (
    (
        "APP_IPHONE_DUO",
        ROOT / "store" / "metadata" / "screenshots" / "ios" / "iphone-duo",
        frozenset({(1398, 2034), (2007, 2853)}),
    ),
)
# Apple Watch screenshots. The set is optional: without PNGs in this folder the
# live Watch sets are left alone. Each frame's size picks its display type
# (Apple's screenshot specifications), so one folder can fill more than one
# Watch set.
WATCH_FOLDER = "watch"
WATCH_SCREENSHOTS = ROOT / "store" / "metadata" / "screenshots" / "ios" / WATCH_FOLDER
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
APP_SCREENSHOT_SET_MAX = 10
SCREENSHOT_PROCESSING_TIMEOUT_SECONDS = 600
SCREENSHOT_PROCESSING_POLL_SECONDS = 10
# PNG chunks that describe colour, copied from a committed screenshot to its RGB
# copy (Pillow writes an ICC profile, iCCP, itself).
PNG_COLOUR_CHUNKS = (b"sRGB", b"gAMA", b"cHRM", b"cICP")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

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

CATALOG_GROUPS = ("nonConsumables", "consumables", "subscriptions")


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


class AscClient:
    # JWTs are minted for 20 minutes; refresh well before that during long waits.
    TOKEN_MAX_AGE_SECONDS = 600

    def __init__(self, token: str | None = None, *, token_factory: Any | None = None) -> None:
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
    ) -> dict[str, Any] | None:
        url = path if path.startswith("http") else f"{ASC_BASE}{path}"
        headers = {"Authorization": self._authorization(), "Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
                return json.loads(payload.decode("utf-8")) if payload else None
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

    def upload_bytes(self, url: str, data: bytes, headers: dict[str, str], *, method: str = "PUT") -> None:
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300):
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            fail(f"ASC asset upload → HTTP {exc.code}: {detail[:1000]}")


class ReadOnlyViolation(AssertionError):
    """A --preflight code path tried to change App Store Connect."""


class ReadOnlyClient:
    """GET-only view of a client for --preflight: any write raises before it is sent."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, path: str) -> dict[str, Any]:
        return self._client.get(path)

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if method.upper() != "GET":
            raise ReadOnlyViolation(f"preflight is read-only; refused {method} {path}")
        return self._client.request(method, path, body=body)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise ReadOnlyViolation(f"preflight is read-only; refused POST {path}")

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise ReadOnlyViolation(f"preflight is read-only; refused PATCH {path}")

    def upload_bytes(self, url: str, data: bytes, headers: dict[str, str], *, method: str = "PUT") -> None:
        raise ReadOnlyViolation(f"preflight is read-only; refused {method} upload")


def make_asc_token(key_id: str, issuer_id: str, private_key: str) -> str:
    import jwt  # noqa: PLC0415 - only live runs need PyJWT

    now = int(time.time())
    payload = {"iss": issuer_id, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"}
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
    return key_id, issuer_id, key_p8.replace("\\n", "\n")


def client_from_environment() -> AscClient:
    """Client whose JWT is re-minted automatically during long-running steps."""
    key_id, issuer_id, key_p8 = load_credentials()
    return AscClient(token_factory=lambda: make_asc_token(key_id, issuer_id, key_p8))


def find_app(client: Any, bundle_id: str, expected_id: str = APP_STORE_APP_ID) -> str:
    """The App Store Connect app id for `bundle_id`; refuses anything but ABYSM's app."""
    query = urllib.parse.urlencode({"filter[bundleId]": bundle_id})
    data = client.get(f"/apps?{query}").get("data") or []
    # filter[bundleId] can match by prefix on some keys, so compare exactly; a row
    # without a bundleId is never taken as a match.
    exact = [row for row in data if (row.get("attributes") or {}).get("bundleId") == bundle_id]
    if not exact:
        fail(f"no App Store Connect app for bundle id {bundle_id}")
    if len(exact) > 1:
        fail(f"ambiguous: {len(exact)} App Store Connect apps for bundle id {bundle_id}")
    app_id = exact[0]["id"]
    if expected_id and app_id != expected_id:
        fail(f"bundle id {bundle_id} resolved to app {app_id}, expected {expected_id}")
    return app_id


def iter_collection(client: Any, path: str) -> list[dict[str, Any]]:
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


def version_state(version: dict[str, Any]) -> str:
    attrs = version.get("attributes") or {}
    return attrs.get("appStoreState") or attrs.get("appVersionState") or ""


def list_ios_app_store_versions(client: Any, app_id: str) -> list[dict[str, Any]]:
    """Every iOS App Store version of the app."""
    return iter_collection(
        client,
        f"/apps/{app_id}/appStoreVersions?"
        + urllib.parse.urlencode({"limit": "50", "filter[platform]": "IOS"}),
    )


def get_ios_app_store_version(client: Any, app_id: str, version_string: str) -> dict[str, Any]:
    rows = list_ios_app_store_versions(client, app_id)
    matches = [r for r in rows if (r.get("attributes") or {}).get("versionString") == version_string]
    if not matches:
        fail(
            f"no iOS App Store version {version_string!r} for app {app_id} "
            "(turn on IOS_CREATE_VERSION, or create the version in App Store Connect first: "
            f"add iOS version {version_string} and leave it in Prepare for Submission, then rerun)"
        )
    if len(matches) > 1:
        fail(f"ambiguous App Store versions for {version_string!r}: {len(matches)} records")
    return matches[0]


def localization_for_locale(client: Any, collection_path: str, locale: str) -> dict[str, Any]:
    rows = iter_collection(client, f"{collection_path}?limit=50")
    for row in rows:
        if (row.get("attributes") or {}).get("locale") == locale:
            return row
    fail(f"no {locale} localization at {collection_path}; add it in App Store Connect first")
    raise AssertionError  # unreachable


# --- Listing text ------------------------------------------------------------


def listing_locales(metadata_dir: Path) -> list[str]:
    """Locales with a folder under store/metadata/ios/ (one per App Store localization)."""
    ios_dir = metadata_dir / "ios"
    if not ios_dir.is_dir():
        return []
    return sorted(p.name for p in ios_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def field_values(loc_dir: Path, fields: Iterable[tuple[str, str, int | None]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for attribute, filename, _ in fields:
        value = read_text(loc_dir / filename)
        if value is not None:
            values[attribute] = value
    return values


def check_field_values(locale: str, values: dict[str, str], fields: Iterable[tuple[str, str, int | None]]) -> None:
    for attribute, filename, limit in fields:
        value = values.get(attribute)
        if value is None:
            continue
        if limit is not None and len(value) > limit:
            fail(f"{locale}/{filename} is {len(value)} characters (App Store limit {limit})")
        if attribute in URL_ATTRIBUTES and not re.fullmatch(r"https://\S+", value):
            fail(f"{locale}/{filename} must be a single https:// URL")


def changed_attributes(current: dict[str, Any], wanted: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in wanted.items() if (current.get(k) or "").strip() != v}


def app_info_state(info: dict[str, Any]) -> str:
    attrs = info.get("attributes") or {}
    return attrs.get("appStoreState") or attrs.get("state") or ""


def choose_app_info(client: Any, app_id: str) -> tuple[dict[str, Any] | None, bool]:
    """The App Info the repo is compared with, and whether a release may change it.

    A version being prepared (or waiting in a draft review submission) has its own
    editable App Info next to the live one. Otherwise the App Info of the version
    in review, or else the live one, is only compared, never changed.
    """
    infos = iter_collection(client, f"/apps/{app_id}/appInfos")
    for info in infos:
        if app_info_state(info) in EDITABLE_APP_INFO_STATES:
            return info, True
    in_flight = [info for info in infos if app_info_state(info) not in LIVE_APP_INFO_STATES]
    chosen = (in_flight or infos or [None])[0]
    return chosen, False


def upload_listing(
    client: Any,
    *,
    app_id: str,
    version_id: str,
    locale: str,
    metadata_dir: Path,
    include_listing: bool = True,
    include_whats_new: bool = False,
    app_info: tuple[dict[str, Any] | None, bool] | None = None,
    text_editable: bool = True,
    write: bool = True,
    state: str = "",
) -> list[str]:
    """Bring one locale's version text (and, with the listing, App Info) in line with the repo.

    Listing text and What's New are separate switches. Only fields that differ are
    sent, so a re-run changes nothing. When the version's text is locked (already
    submitted) or no App Info is editable, a field that differs fails the run
    instead of being skipped. With write=False (preflight) nothing is sent.
    Returns the fields that differ, as "<locale> <attribute>".
    """
    loc_dir = metadata_dir / "ios" / locale
    fields = (LISTING_FIELDS if include_listing else ()) + ((WHATS_NEW_FIELD,) if include_whats_new else ())
    wanted = field_values(loc_dir, fields)
    version_loc = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )
    changes = changed_attributes(version_loc.get("attributes") or {}, wanted)
    differing = [f"{locale} {name}" for name in sorted(changes)]
    if changes and not text_editable:
        fail(
            f"{locale}: {', '.join(sorted(changes))} differ from App Store Connect, but the "
            f"version text cannot change while the version is {state or 'locked'}; make the "
            "repo match App Store Connect, or remove the version from review there, then rerun"
        )
    if changes and write:
        client.patch(
            f"/appStoreVersionLocalizations/{version_loc['id']}",
            {"data": {"type": "appStoreVersionLocalizations", "id": version_loc["id"], "attributes": changes}},
        )
        print(f"  {locale}: updated {', '.join(sorted(changes))}")
    elif changes:
        print(f"  {locale}: would update {', '.join(sorted(changes))}")
    else:
        print(f"  {locale}: version text already matches the repo")

    if not include_listing:
        return differing
    info_wanted = field_values(loc_dir, APP_INFO_FIELDS)
    if not info_wanted:
        return differing
    info, info_editable = app_info if app_info is not None else choose_app_info(client, app_id)
    if info is None:
        fail(f"App Store Connect returned no App Info for app {app_id}")
        raise AssertionError  # unreachable
    info_loc = localization_for_locale(client, f"/appInfos/{info['id']}/appInfoLocalizations", locale)
    info_changes = changed_attributes(info_loc.get("attributes") or {}, info_wanted)
    differing += [f"{locale} {name}" for name in sorted(info_changes)]
    if not info_changes:
        print(f"  {locale}: App Info already matches the repo")
    elif not (info_editable and text_editable):
        # Name, subtitle and privacy URL can only change while a new version is prepared.
        fail(
            f"{locale}: App Info fields {', '.join(sorted(info_changes))} differ from App Store "
            f"Connect but no App Info is editable (state {app_info_state(info) or 'unknown'}); "
            "prepare the new version first"
        )
    elif write:
        client.patch(
            f"/appInfoLocalizations/{info_loc['id']}",
            {"data": {"type": "appInfoLocalizations", "id": info_loc["id"], "attributes": info_changes}},
        )
        print(f"  {locale}: updated App Info {', '.join(sorted(info_changes))}")
    else:
        print(f"  {locale}: would update App Info {', '.join(sorted(info_changes))}")
    return differing


# --- Screenshots ---------------------------------------------------------------


def png_chunks(raw: bytes, *, strict: bool = True) -> list[tuple[bytes, bytes]]:
    """(type, data) of every chunk of a PNG, up to IEND.

    Raises ValueError when the bytes are not a PNG with an IHDR header. Strict
    (the default) also requires a well-formed file: every chunk complete with a
    matching CRC, image data that decompresses to the rows the header describes,
    and a final IEND, so a truncated or corrupt file fails before anything is
    uploaded. Lenient reading (a committed screenshot's size and colour chunks,
    which only need the header) stops at the first chunk that runs past the end
    of the file.
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

    The last guard before upload: staging (stage_screenshots) writes RGB, so a
    file that still stores alpha was not staged by it.
    """
    try:
        chunks = png_chunks(path.read_bytes())
    except ValueError as exc:
        fail(f"the {label} screenshot {path.name} is not a readable PNG ({exc})")
    if png_stores_alpha(chunks):
        fail(
            f"the {label} screenshot {path.name} stores an alpha channel (PNG colour type 4 or 6, "
            "or a tRNS chunk), which App Store Connect refuses; upload the RGB copy "
            "stage_screenshots makes, never the committed file"
        )
    return png_size(chunks)


def shown_path(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def png_file_size(path: Path) -> tuple[int, int]:
    """A committed screenshot's size, from its PNG header."""
    try:
        return png_size(png_chunks(path.read_bytes(), strict=False))
    except ValueError:
        fail(f"{path.name} is not a PNG file")
        raise AssertionError  # unreachable


def checked_set(
    display_type: str, folder: Path, accepted: frozenset[tuple[int, int]], pngs: list[Path]
) -> list[Path]:
    """The set's PNGs once their count and sizes are ones App Store Connect takes."""
    shown = shown_path(folder)
    if len(pngs) > APP_SCREENSHOT_SET_MAX:
        fail(f"{shown}/ has {len(pngs)} PNGs; App Store Connect allows {APP_SCREENSHOT_SET_MAX}")
    sizes = ", ".join(f"{w}x{h}" for w, h in sorted(accepted))
    for path in pngs:
        size = png_file_size(path)
        if size not in accepted:
            fail(f"{shown}/{path.name} is {size[0]}x{size[1]}; {display_type} accepts {sizes}")
    return pngs


def watch_screenshot_sets(watch_dir: Path) -> list[tuple[str, list[Path]]]:
    """The Watch frames grouped by the display type their size belongs to, in file name order."""
    groups: dict[str, list[Path]] = {}
    for path in sorted(watch_dir.glob("*.png")) if watch_dir.is_dir() else []:
        size = png_file_size(path)
        display_type = WATCH_DISPLAY_TYPES.get(size)
        if display_type is None:
            accepted = ", ".join(f"{w}x{h}" for w, h in WATCH_DISPLAY_TYPES)
            fail(
                f"the Apple Watch screenshot {path.name} is {size[0]}x{size[1]}, which is not an "
                f"Apple Watch screenshot size ({accepted})"
            )
        groups.setdefault(display_type, []).append(path)
    return list(groups.items())


def screenshot_plan(
    sets: Iterable[tuple[str, Path, frozenset[tuple[int, int]]]] | None = None,
    *,
    optional_sets: Iterable[tuple[str, Path, frozenset[tuple[int, int]]]] | None = None,
    watch_dir: Path | None = None,
) -> list[tuple[str, list[Path]]]:
    """Validate the App Store-ready images; fail clearly if any set is missing or wrong.

    Every set (iPhone, iPad) needs 1-10 PNGs of a size it accepts. The optional
    sets (iPhone Duo) and the Apple Watch sets follow, Watch sets last, only when
    their folder holds PNGs; each Watch frame's size picks its display type.
    """
    plan: list[tuple[str, list[Path]]] = []
    for display_type, folder, accepted in SCREENSHOT_SETS if sets is None else sets:
        pngs = sorted(folder.glob("*.png")) if folder.is_dir() else []
        if not pngs:
            fail(
                f"IOS_UPLOAD_SCREENSHOTS is on but {shown_path(folder)}/ has no PNG screenshots for "
                f"{display_type}; add App Store-ready images or turn the switch off"
            )
        plan.append((display_type, checked_set(display_type, folder, accepted, pngs)))
    for display_type, folder, accepted in OPTIONAL_SCREENSHOT_SETS if optional_sets is None else optional_sets:
        pngs = sorted(folder.glob("*.png")) if folder.is_dir() else []
        if pngs:
            plan.append((display_type, checked_set(display_type, folder, accepted, pngs)))
    watch = WATCH_SCREENSHOTS if watch_dir is None else watch_dir
    for display_type, pngs in watch_screenshot_sets(watch):
        if len(pngs) > APP_SCREENSHOT_SET_MAX:
            fail(
                f"{shown_path(watch)}/ has {len(pngs)} {WATCH_LABELS[display_type]} PNGs; "
                f"App Store Connect allows {APP_SCREENSHOT_SET_MAX}"
            )
        plan.append((display_type, pngs))
    return plan


def try_import_pillow():
    try:
        from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415 - only screenshot runs need Pillow

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


def prepare_asc_png(Image, path: Path) -> bytes:
    """An RGB PNG of the screenshot, without alpha, colour profile kept."""
    from PIL import PngImagePlugin  # type: ignore[import-untyped]  # noqa: PLC0415

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        icc_profile = img.info.get("icc_profile")
        # Flatten once, so translucent pixels are blended a single time.
        out_img = opaque_rgb(Image, img)
    info = PngImagePlugin.PngInfo()
    for kind, data in colour_chunks(raw):
        info.add(kind, data)
    out = io.BytesIO()
    options = {"icc_profile": icc_profile} if icc_profile else {}
    out_img.save(out, format="PNG", pnginfo=info, **options)
    return out.getvalue()


def stores_alpha(path: Path) -> bool:
    """True when a PNG stores alpha (colour type 4 or 6, or a tRNS chunk)."""
    try:
        chunks = png_chunks(path.read_bytes(), strict=False)
    except ValueError:
        fail(f"not a PNG: {path}")
    return png_stores_alpha(chunks)


def stage_screenshots(plan: list[tuple[str, list[Path]]], staging_dir: Path) -> list[tuple[str, list[Path]]]:
    """RGB copies of the plan's screenshots in staging_dir: the plan the run uploads.

    The committed files are only read, never rewritten. With Pillow every frame
    is saved again as RGB: transparent pixels laid over black, its colour profile
    (ICC profile, sRGB, gamma and chromaticity chunks) kept. Without Pillow a
    file is copied as it is, so one that stores alpha fails here. Every copy
    then passes staged_png, the last guard before upload.
    """
    Image = try_import_pillow()
    staged: list[tuple[str, list[Path]]] = []
    for display_type, pngs in plan:
        dest = staging_dir / display_type
        dest.mkdir(parents=True, exist_ok=True)
        copies = []
        for path in pngs:
            if Image is not None:
                try:
                    data = prepare_asc_png(Image, path)
                except (OSError, SyntaxError, ValueError) as exc:
                    fail(f"{shown_path(path)} is not a readable PNG ({exc})")
            else:
                # Without Pillow the file is copied as it is, so it must already be RGB.
                if stores_alpha(path):
                    fail(
                        f"{shown_path(path)}: App Store screenshots cannot store an alpha channel; "
                        "install Pillow (requirements-store.txt) so the run can upload an RGB copy, "
                        "or re-export it as RGB"
                    )
                data = path.read_bytes()
            copy = dest / path.name
            copy.write_bytes(data)
            staged_png(copy, WATCH_LABELS.get(display_type, display_type))
            copies.append(copy)
        staged.append((display_type, copies))
    return staged


def md5_hex(path: Path) -> str:
    """The checksum App Store Connect stores as an uploaded screenshot's sourceFileChecksum."""
    return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - Apple's checksum


def find_screenshot_set(client: Any, version_loc_id: str, display_type: str) -> dict[str, Any] | None:
    for item in iter_collection(client, f"/appStoreVersionLocalizations/{version_loc_id}/appScreenshotSets"):
        if (item.get("attributes") or {}).get("screenshotDisplayType") == display_type:
            return item
    return None


def ensure_screenshot_set(client: Any, version_loc_id: str, display_type: str) -> dict[str, Any]:
    found = find_screenshot_set(client, version_loc_id, display_type)
    if found is not None:
        return found
    created = client.post(
        "/appScreenshotSets",
        {
            "data": {
                "type": "appScreenshotSets",
                "attributes": {"screenshotDisplayType": display_type},
                "relationships": {
                    "appStoreVersionLocalization": {
                        "data": {"type": "appStoreVersionLocalizations", "id": version_loc_id}
                    }
                },
            }
        },
    )
    return created["data"]


def screenshot_set_problem(client: Any, screenshot_set_id: str, pngs: list[Path]) -> str | None:
    """Why the set does not already hold the repo's images, or None when it does.

    The set matches only when it holds the repo's images (same checksums, same
    order) and App Store Connect has processed every one (assetDeliveryState
    COMPLETE). An upload's sourceFileChecksum is stored when it is committed,
    before Apple processes the image, so a set left by a failed or timed-out wait
    already has the repo's checksums but an image that is FAILED or still
    processing. Such a set must be uploaded again, never taken as done.
    """
    shots = iter_collection(client, f"/appScreenshotSets/{screenshot_set_id}/appScreenshots")
    if [(s.get("attributes") or {}).get("sourceFileChecksum") for s in shots] != [md5_hex(p) for p in pngs]:
        return "differ from the repo"
    unprocessed = []
    for shot in shots:
        attrs = shot.get("attributes") or {}
        state = (attrs.get("assetDeliveryState") or {}).get("state")
        if state != "COMPLETE":
            unprocessed.append(f"{attrs.get('fileName') or shot.get('id')}: {state or 'no processing state'}")
    if unprocessed:
        return f"match the repo but are not all processed by App Store Connect ({', '.join(unprocessed)})"
    return None


def list_screenshot_ids(client: Any, screenshot_set_id: str) -> list[str]:
    return [s["id"] for s in iter_collection(client, f"/appScreenshotSets/{screenshot_set_id}/appScreenshots")]


def send_upload_operations(client: Any, reserved: dict[str, Any], raw: bytes, name: str) -> None:
    """Send a file's bytes the way the reservation's uploadOperations ask for."""
    operations = attrs(reserved).get("uploadOperations") or []
    if not operations:
        fail(f"App Store Connect returned no upload operations for {name}")
    for op in operations:
        offset = int(op.get("offset", 0))
        chunk = raw[offset: offset + int(op["length"])]
        headers = {h["name"]: h["value"] for h in op.get("requestHeaders") or []}
        client.upload_bytes(op["url"], chunk, headers, method=(op.get("method") or "PUT"))


def upload_screenshot_file(client: Any, screenshot_set_id: str, image_path: Path) -> str:
    raw = image_path.read_bytes()
    reserved = client.post(
        "/appScreenshots",
        {
            "data": {
                "type": "appScreenshots",
                "attributes": {"fileName": image_path.name, "fileSize": len(raw)},
                "relationships": {
                    "appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": screenshot_set_id}}
                },
            }
        },
    )
    shot = reserved["data"]
    send_upload_operations(client, shot, raw, image_path.name)
    client.patch(
        f"/appScreenshots/{shot['id']}",
        {
            "data": {
                "type": "appScreenshots",
                "id": shot["id"],
                "attributes": {"uploaded": True, "sourceFileChecksum": hashlib.md5(raw).hexdigest()},  # noqa: S324 - Apple's checksum
            }
        },
    )
    print(f"    uploaded {image_path.name}")
    return shot["id"]


def wait_for_screenshots(
    client: Any,
    screenshot_ids: list[str],
    *,
    timeout: float = SCREENSHOT_PROCESSING_TIMEOUT_SECONDS,
    poll_seconds: float = SCREENSHOT_PROCESSING_POLL_SECONDS,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until App Store Connect has processed every uploaded screenshot."""
    deadline = now() + timeout
    pending = list(screenshot_ids)
    while pending:
        still: list[str] = []
        for shot_id in pending:
            attrs = client.get(f"/appScreenshots/{shot_id}").get("data", {}).get("attributes") or {}
            delivery = attrs.get("assetDeliveryState") or {}
            state = delivery.get("state")
            if state == "COMPLETE":
                continue
            if state == "FAILED":
                errors = "; ".join(e.get("description") or e.get("code") or "?" for e in delivery.get("errors") or [])
                fail(f"App Store Connect rejected screenshot {attrs.get('fileName') or shot_id}: {errors or 'FAILED'}")
            still.append(shot_id)
        pending = still
        if not pending:
            break
        if now() >= deadline:
            fail(f"timed out waiting for App Store Connect to process {len(pending)} screenshot(s)")
        sleep(poll_seconds)


def upload_screenshots(
    client: Any,
    *,
    version_id: str,
    locale: str,
    plan: list[tuple[str, list[Path]]],
    wait: Callable[[Any, list[str]], None] = wait_for_screenshots,
    editable: bool = True,
    write: bool = True,
    state: str = "",
) -> list[str]:
    """Make each display type's set hold exactly the repo's images, in file-name order.

    A set that already holds them (same checksums, same order) and whose images
    App Store Connect has all processed is left alone, so a re-run uploads
    nothing. Any other set, including one with an image that failed or is still
    processing after an earlier attempt, is replaced only while screenshots are
    editable; otherwise the run fails. With write=False (preflight) nothing is
    sent. Optional sets (iPhone Duo, Apple Watch) without staged PNGs are not in
    the plan, so their live sets are left alone. Every file must be a staged RGB
    copy: one that still stores alpha or is not a well-formed PNG fails before
    anything is sent. Returns the display types that differ.
    """
    # The last guard before upload (stage_screenshots ran it too).
    for display_type, pngs in plan:
        for path in pngs:
            staged_png(path, WATCH_LABELS.get(display_type, display_type))
    planned = {display_type for display_type, _ in plan}
    for display_type, _, _ in OPTIONAL_SCREENSHOT_SETS:
        if display_type not in planned:
            print(f"  no {display_type} screenshots staged; that set in App Store Connect is left alone")
    if not planned & set(WATCH_LABELS):
        print("  no Apple Watch screenshots staged; the Watch sets in App Store Connect are left alone")
    version_loc = localization_for_locale(
        client, f"/appStoreVersions/{version_id}/appStoreVersionLocalizations", locale
    )
    differing: list[str] = []
    uploaded: list[str] = []
    for display_type, pngs in plan:
        existing = find_screenshot_set(client, version_loc["id"], display_type)
        problem = "are missing" if existing is None else screenshot_set_problem(client, existing["id"], pngs)
        if problem is None:
            print(f"  {display_type} ({locale}): {len(pngs)} screenshot(s) already match the repo")
            continue
        differing.append(display_type)
        if not editable:
            fail(
                f"{display_type} ({locale}) screenshots {problem}; screenshots cannot change while the "
                f"version is {state or 'locked'}, so remove the version from its review submission in "
                "App Store Connect (or turn IOS_UPLOAD_SCREENSHOTS off), then rerun"
            )
        if not write:
            print(f"  {display_type} ({locale}): screenshots {problem}; would replace the set with {len(pngs)} screenshot(s)")
            continue
        print(f"  {display_type} ({locale}): screenshots {problem}; replacing the set with {len(pngs)} screenshot(s)")
        if existing is None:
            existing = ensure_screenshot_set(client, version_loc["id"], display_type)
        set_id = existing["id"]
        old_ids = list_screenshot_ids(client, set_id)
        # Upload replacements one at a time. App Store Connect rejects new
        # reservations while a set is full, so delete one old screenshot first
        # when needed. A failure mid-way can leave a mixed set; rerun to repair.
        for path in pngs:
            if len(list_screenshot_ids(client, set_id)) >= APP_SCREENSHOT_SET_MAX:
                if not old_ids:
                    fail(f"{display_type} set is full with no previous screenshots left to replace")
                client.request("DELETE", f"/appScreenshots/{old_ids.pop(0)}")
            uploaded.append(upload_screenshot_file(client, set_id, path))
        for shot_id in old_ids:
            client.request("DELETE", f"/appScreenshots/{shot_id}")
        if old_ids:
            print(f"    removed {len(old_ids)} previous screenshot(s)")
    if uploaded:
        wait(client, uploaded)
        print(f"  {len(uploaded)} screenshot(s) processed by App Store Connect")
    return differing


# --- In-app purchases ----------------------------------------------------------


def load_catalog(path: Path = CATALOG) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def intended_product_ids(catalog: dict[str, Any] | None = None) -> set[str]:
    """Product ids the release may submit, from store/catalog/products.json."""
    catalog = load_catalog() if catalog is None else catalog
    ids: set[str] = set()
    for group in CATALOG_GROUPS:
        for item in catalog.get(group) or []:
            pid = item.get("id")
            if isinstance(pid, str) and pid:
                ids.add(pid)
    return ids


class ProductSubmitError(Exception):
    """A required product could not be added to App Review."""


class ProductAttachResult:
    """Catalog products in a draft review submission after attach_catalog_products()."""

    def __init__(self, submission_id: str, attached: int, added_items: bool) -> None:
        self.submission_id = submission_id
        # Products in the draft now, attached by this call or by an earlier attempt.
        self.attached = attached
        self.added_items = added_items


# Subscription states that can still be (re)attached to a review.
SUBMITTABLE_SUBSCRIPTION_STATES = {"READY_TO_SUBMIT", "DEVELOPER_REJECTED", "READY_FOR_REVIEW"}
# One-time purchases ("Unlock the Deep") are attached only while Ready to Submit.
SUBMITTABLE_IAP_STATES = {"READY_TO_SUBMIT"}


def draft_submission_query(app_id: str) -> str:
    return f"/apps/{app_id}/reviewSubmissions?" + urllib.parse.urlencode(
        {"filter[state]": "READY_FOR_REVIEW", "limit": "5"}
    )


def draft_submission_ids(client: Any, app_id: str) -> list[str]:
    """The app's READY_FOR_REVIEW (draft, not yet submitted) review submissions."""
    return [row["id"] for row in iter_collection(client, draft_submission_query(app_id))]


def find_draft_submission(client: Any, app_id: str, version_id: str | None = None) -> str | None:
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


def create_draft_submission(client: Any, app_id: str) -> str:
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


def get_or_create_draft_submission(client: Any, app_id: str) -> str:
    """The app's draft review submission for versions and products (see find_draft_submission), creating one."""
    return find_draft_submission(client, app_id) or create_draft_submission(client, app_id)


def version_draft(client: Any, app_id: str, version_id: str, version_state: str) -> str | None:
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


def reserve_version_draft(client: Any, app_id: str, version_id: str, version_state: str) -> str | None:
    """Pick and check the version's draft before anything is written, and reserve it.

    The draft is kept for the version and its products: creative images never take it.
    """
    draft = version_draft(client, app_id, version_id, version_state)
    if draft:
        print(f"  review submission {draft} is kept for the version and its products")
    return draft


def draft_items_path(submission_id: str) -> str:
    # Review Submission Items omit relationships unless they are included, so
    # every relationship the readers look at (versions, products and Asset
    # Library images) is asked for explicitly.
    return f"/reviewSubmissions/{submission_id}/items?limit=50&include={DRAFT_ITEM_INCLUDE}"


def draft_item_relationships(client: Any, submission_id: str) -> list[dict[str, Any]]:
    """The draft's items with their relationships populated."""
    return iter_collection(client, draft_items_path(submission_id))


def draft_item_version_ids(client: Any, submission_id: str) -> set[str]:
    """App, subscription and in-app purchase version ids already attached to a draft."""
    ids: set[str] = set()
    for item in draft_item_relationships(client, submission_id):
        for name in ("subscriptionVersion", "appStoreVersion", "inAppPurchaseVersion"):
            related = ((item.get("relationships") or {}).get(name) or {}).get("data") or {}
            if related.get("id"):
                ids.add(related["id"])
    return ids


def submit_review_submission(client: Any, submission_id: str) -> None:
    """Submitting is a PATCH with submitted=true; there is no /submit action."""
    client.request(
        "PATCH",
        f"/reviewSubmissions/{submission_id}",
        body={"data": {"type": "reviewSubmissions", "id": submission_id, "attributes": {"submitted": True}}},
    )


def newest_version_id(versions: list[dict[str, Any]]) -> str | None:
    """A product's in-flight version (one per product; the highest number wins)."""
    numbered = [v for v in versions if (v.get("attributes") or {}).get("version")]
    if not numbered:
        return versions[0]["id"] if versions else None
    return max(numbered, key=lambda v: v["attributes"]["version"])["id"]


def current_subscription_version(client: Any, subscription_id: str) -> str | None:
    return newest_version_id(iter_collection(client, f"/subscriptions/{subscription_id}/versions"))


def iap_versions_path(iap_id: str) -> str:
    # In-app purchase versions are a v2 resource.
    return f"{ASC_ROOT}/v2/inAppPurchases/{iap_id}/versions?limit=50"


def current_iap_version(client: Any, iap_id: str) -> str | None:
    return newest_version_id(iter_collection(client, iap_versions_path(iap_id)))


def catalog_review_items(
    client: Any, app_id: str, intended_ids: set[str]
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Ready-to-Submit catalog products and the review item each needs (GET requests only).

    Returns ([(label, relationship, resource type, version id)], problems). Only
    products listed in store/catalog/products.json are considered, and approved
    products are never resubmitted.
    """
    items: list[tuple[str, str, str, str]] = []
    problems: list[str] = []
    for group in iter_collection(client, f"/apps/{app_id}/subscriptionGroups?limit=200"):
        for sub in iter_collection(client, f"/subscriptionGroups/{group['id']}/subscriptions?limit=200"):
            attrs = sub.get("attributes") or {}
            pid = attrs.get("productId") or ""
            if pid not in intended_ids or attrs.get("state") not in SUBMITTABLE_SUBSCRIPTION_STATES:
                continue
            label = f"subscription {attrs.get('name') or sub['id']} ({pid})"
            version_id = current_subscription_version(client, sub["id"])
            if version_id:
                items.append((label, "subscriptionVersion", "subscriptionVersions", version_id))
            else:
                problems.append(f"{label}: no version to submit")
    for iap in iter_collection(client, f"/apps/{app_id}/inAppPurchasesV2?limit=200"):
        attrs = iap.get("attributes") or {}
        pid = attrs.get("productId") or ""
        if pid not in intended_ids or attrs.get("state") not in SUBMITTABLE_IAP_STATES:
            continue
        label = f"in-app purchase {attrs.get('name') or iap['id']} ({pid})"
        version_id = current_iap_version(client, iap["id"])
        if version_id:
            items.append((label, "inAppPurchaseVersion", "inAppPurchaseVersions", version_id))
        else:
            problems.append(f"{label}: no version to submit")
    return items, problems


def attach_catalog_products(
    client: Any,
    app_id: str,
    *,
    intended_ids: set[str] | None = None,
    submission_id: str | None = None,
) -> ProductAttachResult:
    """Attach Ready-to-Submit catalog products to the app's draft review submission.

    Nothing goes to App Review here. Subscriptions (`subscriptionVersion`) and
    one-time purchases (`inAppPurchaseVersion`; ABYSM's "Unlock the Deep" is a
    non-consumable) become items of the same draft as the app version, and the
    single submit after the final build recheck sends them all together. The
    deprecated `inAppPurchaseSubmissions` endpoint, which sends a product to review
    on its own straight away, is never used. Items already in the draft are
    skipped, so a retry adds nothing twice. Without a submission_id the app's
    existing draft is used, and a draft is created only when something needs one.
    """
    if intended_ids is None:
        intended_ids = intended_product_ids()
    if not intended_ids:
        print("  catalog has no products to submit")
        return ProductAttachResult(submission_id or "", 0, False)
    items, failures = catalog_review_items(client, app_id, intended_ids)
    if submission_id is None:
        submission_id = find_draft_submission(client, app_id)
    already_attached = draft_item_version_ids(client, submission_id) if submission_id else set()
    attached = 0
    added_items = False
    for label, relationship, resource_type, version_id in items:
        if version_id in already_attached:
            attached += 1
            print(f"  {label}: already in the review submission")
            continue
        if submission_id is None:
            submission_id = create_draft_submission(client, app_id)
        try:
            client.post(
                "/reviewSubmissionItems",
                {
                    "data": {
                        "type": "reviewSubmissionItems",
                        "relationships": {
                            "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                            relationship: {"data": {"type": resource_type, "id": version_id}},
                        },
                    }
                },
            )
        except SystemExit:
            failures.append(label)
            continue
        attached += 1
        added_items = True
        print(f"  {label}: attached to the review submission")

    print(f"  catalog products in the review submission: {attached}")
    if failures:
        raise ProductSubmitError(
            "these required products were not added to App Review: "
            + "; ".join(failures)
            + ". Resolve them in App Store Connect and rerun the release."
        )
    return ProductAttachResult(submission_id or "", attached, added_items)


# --- Build selection and review ------------------------------------------------

RELEASE_TYPE_PUBLIC = "AFTER_APPROVAL"
RELEASE_TYPE_MANUAL = "MANUAL"


def verify_build(client: Any, build_id: str, version_string: str, build_number: str) -> None:
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
    pre = next((r for r in payload.get("included") or [] if r.get("type") == "preReleaseVersions"), {})
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
        fail(f"build {build_id} is not {version_string} ({build_number}), VALID and unexpired: " + ", ".join(problems))


def attached_build_id(client: Any, version_id: str) -> str | None:
    return (client.get(f"/appStoreVersions/{version_id}/build").get("data") or {}).get("id")


def confirm_selected_build(client: Any, version_id: str, build_id: str) -> None:
    current = attached_build_id(client, version_id)
    if current != build_id:
        fail(f"App Store version {version_id} has build {current or 'none'} selected, expected {build_id}; not submitting")


def select_build(client: Any, version_id: str, build_id: str, version_string: str, build_number: str) -> None:
    """Explicitly select this run's build for the version, then read it back."""
    verify_build(client, build_id, version_string, build_number)
    if attached_build_id(client, version_id) == build_id:
        # A re-run: the version (possibly already in a draft submission) holds it.
        print(f"  build {build_number} ({build_id}) is already selected for version {version_string}")
    else:
        client.request(
            "PATCH",
            f"/appStoreVersions/{version_id}/relationships/build",
            body={"data": {"type": "builds", "id": build_id}},
        )
    confirm_selected_build(client, version_id, build_id)
    print(f"  selected build {build_number} ({build_id}) for version {version_string}")


def set_release_type(client: Any, version_id: str, *, public_release: bool, current: str | None = None) -> None:
    release_type = RELEASE_TYPE_PUBLIC if public_release else RELEASE_TYPE_MANUAL
    label = "automatic after approval" if public_release else "manual release after approval"
    if current == release_type:
        print(f"  release type already {label}")
        return
    client.patch(
        f"/appStoreVersions/{version_id}",
        {"data": {"type": "appStoreVersions", "id": version_id, "attributes": {"releaseType": release_type}}},
    )
    print(f"  release type: {label}")


def submit_for_review(client: Any, app_id: str, version_id: str, state: str = "", *, submission_id: str | None = None) -> None:
    # Reuse a draft (an earlier attempt can leave one that was never submitted).
    if submission_id is None:
        submission_id = get_or_create_draft_submission(client, app_id)
    already_added = state == "READY_FOR_REVIEW" or version_id in draft_item_version_ids(client, submission_id)
    if not already_added:
        client.post(
            "/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                    },
                }
            },
        )
    submit_review_submission(client, submission_id)
    print(f"  submitted App Store version {version_id} for review (submission {submission_id})")


# --- Product page header and search results images -------------------------------


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
    Connect would refuse stops the run before any call. Empty folders mean
    nothing to do: ABYSM has no header or search results image yet.
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


def asset_library_id(client: Any, app_id: str) -> str:
    library_id = (client.get(f"/apps/{app_id}/assetLibrary").get("data") or {}).get("id")
    if not library_id:
        fail(f"App Store Connect returned no Asset Library for app {app_id}")
    return library_id


def library_images(client: Any, library_id: str) -> list[dict[str, Any]]:
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


def wait_for_library_image(client: Any, image_id: str, name: str) -> dict[str, Any]:
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


def upload_library_image(client: Any, library_id: str, path: Path) -> dict[str, Any]:
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
    def __init__(self, library_id: str, localization_id: str, steps: list[CreativeStep]) -> None:
        self.library_id = library_id
        self.localization_id = localization_id
        self.steps = steps


def image_placement_ids(client: Any, image_id: str) -> set[str]:
    return {
        row["id"]
        for row in iter_collection(client, f"/appAssetLibraryImages/{image_id}/relationships/placements?limit=50")
    }


def slot_placements(client: Any, localization_id: str, placement_type: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"filter[placementType]": placement_type, "limit": "50"})
    rows = iter_collection(client, f"/appStoreVersionLocalizations/{localization_id}/placements?{query}")
    return [row for row in rows if attrs(row).get("placementType", placement_type) == placement_type]


def plan_creative_assets(
    client: Any,
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
    cannot resolve. Without any image nothing is asked of App Store Connect.
    """
    sources = creative_sources(creative_dir)
    if not sources:
        return CreativePlan("", "", [])
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


# What the preflight reports for each step the store step would write.
CREATIVE_PREVIEW = {
    "upload": "upload {name} to the Asset Library and send it to App Review on its own",
    "review": "send {name} to App Review on its own",
    "place": "place {name} on the product page",
}


def place_creative_image(client: Any, localization_id: str, step: CreativeStep) -> None:
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
    client: Any, plan: CreativePlan, *, app_id: str, reserved_draft: str | None = None
) -> dict[str, str]:
    """Upload, review and place what plan_creative_assets decided. Returns placement type -> outcome."""
    outcomes: dict[str, str] = {}
    to_review: list[tuple[str, dict[str, Any]]] = []
    for step in plan.steps:
        if step.action == "upload":
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
            place_creative_image(client, plan.localization_id, step)
            outcomes[step.placement_type] = "placed"
        elif step.action in {"pending", "placed", "locked"}:
            outcomes[step.placement_type] = step.action
    return outcomes


def submit_creative_review(
    client: Any,
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
    submit_review_submission(client, submission_id)
    print(f"  submitted creative image review submission {submission_id}")
    return submission_id


# --- Entry point -----------------------------------------------------------------


def validate_local_inputs(
    *,
    upload_listing: bool,
    upload_whats_new: bool,
    upload_screenshots: bool,
    metadata_dir: Path,
    screenshot_sets: Iterable[tuple[str, Path, frozenset[tuple[int, int]]]] | None = None,
    staging_dir: Path | None = None,
    upload_creative: bool = False,
    creative_dir: Path | None = None,
) -> tuple[list[str], list[tuple[str, list[Path]]]]:
    """Check every file a live run would send before any App Store Connect call.

    With screenshots on, the plan returned holds the RGB copies staged in
    staging_dir, the files the run uploads; the committed files are only read.
    """
    locales = listing_locales(metadata_dir)
    if (upload_listing or upload_whats_new) and not locales:
        fail(f"no locale folders under {metadata_dir / 'ios'}")
    if upload_listing and PRIMARY_LOCALE not in locales:
        fail(f"the primary locale {PRIMARY_LOCALE} is missing from {metadata_dir / 'ios'}")
    for locale in locales:
        loc_dir = metadata_dir / "ios" / locale
        if upload_listing:
            values = field_values(loc_dir, LISTING_FIELDS)
            if "description" not in values or "keywords" not in values:
                fail(f"{locale} needs description.txt and keywords.txt")
            check_field_values(locale, values, LISTING_FIELDS)
            check_field_values(locale, field_values(loc_dir, APP_INFO_FIELDS), APP_INFO_FIELDS)
        if upload_whats_new:
            value = read_text(loc_dir / WHATS_NEW_FIELD[1])
            if not value:
                fail(f"What's New upload is on, but {locale}/{WHATS_NEW_FIELD[1]} is missing or empty")
            check_field_values(locale, {WHATS_NEW_FIELD[0]: value}, (WHATS_NEW_FIELD,))
    plan: list[tuple[str, list[Path]]] = []
    if upload_screenshots:
        if staging_dir is None:
            raise ValueError("screenshot uploads need a staging_dir for their RGB copies")
        plan = stage_screenshots(screenshot_plan(screenshot_sets), staging_dir)
    if upload_creative:
        creative_sources(creative_dir or DEFAULT_CREATIVE_DIR)
    return locales, plan


def version_key(version_string: str) -> tuple[int, ...] | None:
    """1.0.1 -> (1, 0, 1), padded to three parts; None when it is not a plain version."""
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){0,2}", version_string or ""):
        return None
    parts = [int(part) for part in version_string.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def ensure_ios_app_store_version(client: Any, *, bundle_id: str, version_string: str) -> None:
    """Create the App Store version for this release when it does not exist yet.

    An existing version with this number is left as it is (the preflight then
    checks its state). Nothing is created when another version is still on its
    way to the store, or when this number is not newer than every existing
    version; the created version is read back before the run continues.
    """
    wanted = version_key(version_string)
    if wanted is None:
        fail(f"version {version_string!r} must look like 1.0 or 1.0.1")
    app_id = find_app(client, bundle_id)  # refuses any app but ABYSM's
    versions = list_ios_app_store_versions(client, app_id)
    existing = [row for row in versions if (row.get("attributes") or {}).get("versionString") == version_string]
    if len(existing) > 1:
        fail(f"ambiguous App Store versions for {version_string!r}: {len(existing)} records")
    if existing:
        print(f"App Store version {version_string} already exists ({version_state(existing[0])}); nothing to create")
        return
    problems: list[str] = []
    for row in versions:
        other = (row.get("attributes") or {}).get("versionString") or ""
        state = version_state(row)
        if state not in FINISHED_VERSION_STATES:
            problems.append(
                f"version {other!r} is still {state or 'in an unknown state'}; release or remove it "
                f"in App Store Connect before {version_string} can be created"
            )
        other_key = version_key(other)
        if other_key is not None and other_key >= wanted:
            problems.append(f"version {version_string} is not newer than the existing version {other}")
    if problems:
        fail(f"not creating App Store version {version_string} (nothing was changed):\n  " + "\n  ".join(problems))
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
    # Read back with its own check: the version was just created, so the generic
    # "version is missing" advice (turn on IOS_CREATE_VERSION) would be wrong here.
    created = [
        row
        for row in list_ios_app_store_versions(client, app_id)
        if (row.get("attributes") or {}).get("versionString") == version_string
    ]
    if len(created) != 1:
        fail(
            f"created App Store version {version_string}, but reading it back found {len(created)} records; "
            "check it in App Store Connect before rerunning"
        )
    print(f"created App Store version {version_string} ({version_state(created[0]) or 'state not reported yet'})")


def check_version_state(
    version_string: str, state: str, *, edits_metadata: bool, do_submit: bool, new_build: bool
) -> None:
    """Fail unless the enabled steps can run while the App Store version is in `state`.

    new_build: this run has not uploaded its build yet (--preflight), so a version
    that is already submitted can never take it.
    """
    submitted = state in SUBMITTED_VERSION_STATES
    if do_submit and submitted and new_build:
        fail(
            f"version {version_string!r} is already {state}, so the build this run would upload "
            "cannot be submitted. To finish the run that submitted it, use Re-run failed jobs on "
            "that run; otherwise remove the version from review in App Store Connect first"
        )
    if do_submit and not (submitted or state in SUBMITTABLE_VERSION_STATES):
        fail(f"version {version_string!r} cannot be submitted for review (state={state})")
    if edits_metadata and not (submitted or state in TEXT_EDITABLE_VERSION_STATES):
        fail(
            f"version {version_string!r} is not editable (state={state}); editable states: "
            + ", ".join(sorted(TEXT_EDITABLE_VERSION_STATES))
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata-dir", type=Path, default=METADATA_DIR, help="Listing tree (store/metadata)")
    parser.add_argument(
        "--creative-dir",
        type=Path,
        default=DEFAULT_CREATIVE_DIR,
        help="Product page header and search results images, one folder per slot: "
        + ", ".join(folder for folder, _, _, _ in CREATIVE_SLOTS),
    )
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--version", help="Marketing version from the release tag (v1.0.1 → 1.0.1)")
    parser.add_argument("--build-id", help="App Store Connect id of the build uploaded by this run (required to submit)")
    parser.add_argument("--build-number", help="CFBundleVersion of the build uploaded by this run (required to submit)")
    parser.add_argument("--dry-run", action="store_true", help="Validate switches and local files only — no network")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Read-only App Store Connect checks before anything is built (GET requests only)",
    )
    parser.add_argument(
        "--create-version",
        action="store_true",
        help="Create the App Store version for --version when it is missing (CREATE_IOS_VERSION)",
    )
    args = parser.parse_args()
    if args.dry_run and args.preflight:
        fail("--dry-run and --preflight are separate modes; pass only one")

    if args.create_version:
        if args.preflight:
            fail("--create-version and --preflight are separate modes; pass only one")
        if not truthy("CREATE_IOS_VERSION"):
            print("App Store version creation skipped — CREATE_IOS_VERSION is off")
            return
        if not args.version:
            fail("--create-version needs --version (the marketing version from the tag)")
        # Checked before the dry run returns, so a dry run rejects what a live run would.
        if version_key(args.version) is None:
            fail(f"version {args.version!r} must look like 1.0 or 1.0.1")
        if args.dry_run:
            print(f"App Store Connect dry run — would create App Store version {args.version} if it is missing")
            return
        ensure_ios_app_store_version(client_from_environment(), bundle_id=args.bundle_id, version_string=args.version)
        return

    # The committed screenshots are only read: the run uploads RGB copies made here.
    with tempfile.TemporaryDirectory(prefix="abysm-screenshots-") as staging:
        sync(args, Path(staging))


def sync(args: argparse.Namespace, staging_dir: Path) -> None:
    """The dry run, the read-only preflight or the store step, with screenshot copies in staging_dir."""
    do_listing = truthy("UPLOAD_LISTING")
    do_whats_new = truthy("UPLOAD_WHATS_NEW")
    do_screenshots = truthy("UPLOAD_SCREENSHOTS")
    do_creative = truthy("UPLOAD_CREATIVE_ASSETS")
    do_submit = truthy("SUBMIT_IOS_REVIEW")
    do_public = truthy("PUBLIC_RELEASE")

    if not (do_listing or do_whats_new or do_screenshots or do_creative or do_submit):
        print(
            "App Store Connect step skipped — UPLOAD_LISTING / UPLOAD_WHATS_NEW / UPLOAD_SCREENSHOTS / "
            "UPLOAD_CREATIVE_ASSETS / SUBMIT_IOS_REVIEW are off"
        )
        return
    if do_public and not do_submit:
        fail("PUBLIC_RELEASE only applies when SUBMIT_IOS_REVIEW is on")
    # The preflight runs before this run's build exists.
    if do_submit and not args.preflight and not (args.build_id and args.build_number):
        fail("submitting needs --build-id and --build-number for the build uploaded by this run; an older build is never substituted")

    locales, plan = validate_local_inputs(
        upload_listing=do_listing,
        upload_whats_new=do_whats_new,
        upload_screenshots=do_screenshots,
        metadata_dir=args.metadata_dir,
        screenshot_sets=SCREENSHOT_SETS,
        staging_dir=staging_dir,
        upload_creative=do_creative,
        creative_dir=args.creative_dir,
    )
    if do_submit:
        intended_product_ids()  # the catalog must parse before anything is submitted

    if args.dry_run:
        print("App Store Connect dry run — local inputs OK; no API calls")
        print(
            f"  bundle={args.bundle_id} locales={','.join(locales) or '-'} listing={do_listing} "
            f"whats_new={do_whats_new} screenshots={sum(len(p) for _, p in plan)} "
            f"creative={do_creative} submit={do_submit} public_release={do_public}"
        )
        return
    if not args.version:
        fail("--version is required for live App Store Connect calls")

    client: Any = client_from_environment()
    if args.preflight:
        client = ReadOnlyClient(client)
    write = not args.preflight
    app_id = find_app(client, args.bundle_id)
    version = get_ios_app_store_version(client, app_id, args.version)
    version_id = version["id"]
    state = version_state(version)
    label = "App Store Connect preflight (read-only)" if args.preflight else "App Store Connect"
    print(f"{label}: app={app_id} version={args.version} id={version_id} state={state}")
    edits_metadata = do_listing or do_whats_new or do_screenshots or do_creative
    check_version_state(
        args.version, state, edits_metadata=edits_metadata, do_submit=do_submit, new_build=args.preflight
    )
    submitted = state in SUBMITTED_VERSION_STATES

    if do_submit and submitted:
        # A re-run after this run's submit went through (for example the job failed
        # afterwards): accept it only if review holds exactly this run's build.
        confirm_selected_build(client, version_id, args.build_id)

    # Pick and check the version's draft before any write. It is reserved for
    # the version and its products, so header and search results images never
    # go out in it.
    reserved_draft: str | None = None
    if do_submit and not submitted:
        pick = version_draft if args.preflight else reserve_version_draft
        reserved_draft = pick(client, app_id, version_id, state)

    # Read-only: what each header / search results image needs, decided before any write.
    creative_plan: CreativePlan | None = None
    if do_creative:
        print("checking product page header and search results images…")
        creative_plan = plan_creative_assets(
            client,
            app_id=app_id,
            version_id=version_id,
            locale=PRIMARY_LOCALE,
            creative_dir=args.creative_dir,
            editable=state in EDITABLE_VERSION_STATES,
        )

    if do_listing or do_whats_new:
        parts = [n for n, on in (("listing", do_listing), ("What's New", do_whats_new)) if on]
        print(f"{'checking' if args.preflight else 'updating'} {' + '.join(parts)} for {', '.join(locales)}…")
        app_info = choose_app_info(client, app_id) if do_listing else None
        for locale in locales:
            upload_listing(
                client,
                app_id=app_id,
                version_id=version_id,
                locale=locale,
                metadata_dir=args.metadata_dir,
                include_listing=do_listing,
                include_whats_new=do_whats_new,
                app_info=app_info,
                text_editable=state in TEXT_EDITABLE_VERSION_STATES,
                write=write,
                state=state,
            )

    if do_screenshots:
        print("checking screenshots…" if args.preflight else "uploading screenshots…")
        upload_screenshots(
            client,
            version_id=version_id,
            locale=PRIMARY_LOCALE,
            plan=plan,
            editable=state in EDITABLE_VERSION_STATES,
            write=write,
            state=state,
        )

    if creative_plan is not None and write:
        print("syncing product page header and search results images…")
        apply_creative_plan(client, creative_plan, app_id=app_id, reserved_draft=reserved_draft)
    elif creative_plan is not None:
        for step in creative_plan.steps:
            if step.action in CREATIVE_PREVIEW:
                print(f"  {step.label}: would {CREATIVE_PREVIEW[step.action].format(name=step.path.name)}")

    if args.preflight:
        if do_submit:
            items, problems = catalog_review_items(client, app_id, intended_product_ids())
            if problems:
                fail(
                    "these required products cannot be added to App Review: " + "; ".join(problems)
                    + ". Resolve them in App Store Connect before tagging."
                )
            for item_label, *_ in items:
                print(f"  {item_label}: goes to App Review with the version")
        print(f"preflight passed: version {args.version} ({state}) is ready for the enabled steps; nothing was changed")
        return

    if do_submit and submitted:
        # Nothing is written once the version is submitted. A catalog product still
        # due for review did not go out with the version, and it is never sent to
        # App Review on its own (no draft, no item, no product-only submission).
        items, problems = catalog_review_items(client, app_id, intended_product_ids())
        left_behind = [item_label for item_label, *_ in items] + problems
        if left_behind:
            fail(
                f"version {args.version!r} is already {state}, but these catalog products did not go to App "
                "Review with it: " + "; ".join(left_behind) + ". A product is never sent to App Review without "
                "the version: submit it in App Store Connect yourself (or with the next version), then rerun"
            )
        print(f"submit skipped — the version is already {state} with this run's build; nothing was changed")
    elif do_submit:
        print("selecting this run's build…")
        select_build(client, version_id, args.build_id, args.version, args.build_number)
        set_release_type(
            client, version_id, public_release=do_public, current=(version.get("attributes") or {}).get("releaseType")
        )
        print("attaching catalog products to the review submission…")
        submission_id = reserved_draft or create_draft_submission(client, app_id)
        try:
            result = attach_catalog_products(client, app_id, submission_id=submission_id)
        except ProductSubmitError as exc:
            # A product that must ship with this version blocks the submission.
            fail(f"{exc} Not submitting the app version.")
        # Recheck immediately before submitting: the selection must still be this
        # run's processed build. Nothing has gone to App Review yet; the submit
        # below sends the version and its products together.
        verify_build(client, args.build_id, args.version, args.build_number)
        confirm_selected_build(client, version_id, args.build_id)
        print("submitting for App Review…")
        submit_for_review(client, app_id, version_id, state, submission_id=submission_id)
        print(f"  {result.attached} catalog product(s) went out with the version")

    print("App Store Connect step finished")


if __name__ == "__main__":
    main()
