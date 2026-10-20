"""App Store Connect fakes and helpers, moved verbatim out of fud-ai's scripts/store/test_ios_release.py.

The ported tests, the parity suite and the plan tests share them. Only the
imports above this docstring's code changed: the modules now come from the
store_release package, configured for the synthetic Fud AI tree first (some
defaults below read Fud AI's configured constants when the classes are defined).
"""
from __future__ import annotations

import functools
import io
import json
import urllib.parse
import zlib
from contextlib import redirect_stderr, redirect_stdout

from store_release.ios import asc as asc_release
from store_release.ios import release as ios_release
from support.apps import configure_fud_ai_defaults

configure_fud_ai_defaults()


TAG = "ios-v7.2"


VERSION = "7.2"


def make_png(width: int, height: int, colour_type: int = 2, *, chunks: tuple[tuple[bytes, bytes], ...] = ()) -> bytes:
    """A valid PNG of black pixels (8 bits per sample), built without Pillow.

    chunks are extra (type, data) chunks written before the image data, e.g. tRNS.
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, colour_type, 0, 0, 0])
    extra = b"".join(chunk(kind, data) for kind, data in chunks)
    palette = chunk(b"PLTE", b"\x00\x00\x00") if colour_type == 3 else b""
    pixels = zlib.compress((b"\x00" + b"\x00" * (width * channels)) * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + palette + extra
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


APP = "APP1"


STARTED = 1_800_000_000.0  # this run's upload start (epoch seconds)


def iso(epoch: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def build_row(
    build_id: str = "B1",
    *,
    number: str = "40",
    state: str = "VALID",
    expired: bool = False,
    uploaded: float | None = STARTED + 60,
    uploaded_raw: object = None,
    app: str = APP,
    pre: str = "P1",
) -> dict:
    attributes = {"version": number, "processingState": state, "expired": expired}
    if uploaded_raw is not None:
        attributes["uploadedDate"] = uploaded_raw
    elif uploaded is not None:
        attributes["uploadedDate"] = iso(uploaded)
    return {
        "type": "builds",
        "id": build_id,
        "attributes": attributes,
        "relationships": {
            "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": pre}},
            "app": {"data": {"type": "apps", "id": app}},
        },
    }


def pre_row(pre_id: str = "P1", version: str = VERSION, platform: str = "IOS") -> dict:
    return {"type": "preReleaseVersions", "id": pre_id, "attributes": {"version": version, "platform": platform}}


class FakeClock:
    def __init__(self, start: float) -> None:
        self.time = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


class SequenceClient:
    """Returns queued /builds payloads (or raises queued exceptions) per GET."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.paths: list[str] = []

    def get(self, path: str) -> dict:
        self.paths.append(path)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


class SelectionClient:
    """Fake ASC client for build verification and selection."""

    def __init__(self, *, build: dict, included: list, attached: str | None, sticky: bool = True) -> None:
        self.build = build
        self.included = included
        self.attached = attached
        self.sticky = sticky
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path, None))
        if path.startswith(f"/builds/{self.build['id']}"):
            return {"data": self.build, "included": self.included}
        if path.endswith("/build"):
            return {"data": {"type": "builds", "id": self.attached} if self.attached else None}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path, body))
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        if method == "PATCH" and path.endswith("/relationships/build") and self.sticky:
            self.attached = body["data"]["id"]
        return None


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class FakeAsc:
    """App Store Connect stand-in for asc_release.main() (live path, no network)."""

    def __init__(self, *, state: str = "PREPARE_FOR_SUBMISSION", attached: str | None = "OLD",
                 revert_after_select: bool = False, live_text: dict | None = None,
                 live_shots: list[dict] | None = None, iaps: list[dict] | None = None,
                 live_by_display: dict[str, list[dict]] | None = None) -> None:
        self.state = state
        # display type -> the screenshots its live set holds (replaces live_shots when given).
        self.live_by_display = live_by_display
        self.attached = attached
        self.revert_after_select = revert_after_select
        self.live_text = dict(live_text or {})
        self.live_shots = live_shots
        self.iaps = iaps or []
        self.build_reads = 0
        self.calls: list[tuple[str, str]] = []
        self.created_sets: list[str] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if path.startswith("/apps?"):
            return {"data": [{"id": APP, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}]}
        if path.startswith(f"/apps/{APP}/appStoreVersions"):
            return {"data": [{"id": "V1", "attributes": {"versionString": VERSION, "appStoreState": self.state}}]}
        if path.startswith("/builds/B9"):
            return {"data": build_row("B9"), "included": [pre_row()]}
        if path == "/appStoreVersions/V1/build":
            self.build_reads += 1
            if self.revert_after_select and self.build_reads >= 2:
                return {"data": {"type": "builds", "id": "OLD"}}
            return {"data": {"type": "builds", "id": self.attached} if self.attached else None}
        if path.startswith(f"/apps/{APP}/reviewSubmissions"):
            return {"data": [{"id": "SUB1"}]}
        if path.startswith("/reviewSubmissions/SUB1/items"):
            # A READY_FOR_REVIEW version sits in its draft, as in App Store Connect.
            held = [{"id": "ITEM-V1", "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "V1"}}}}]
            return {"data": held if self.state == "READY_FOR_REVIEW" else []}
        if path.startswith("/appStoreVersions/V1/appStoreVersionLocalizations"):
            return {"data": [{"id": "LOC1", "attributes": {"locale": "en-US", **self.live_text}}]}
        if path == "/appStoreVersionLocalizations/LOC1/appScreenshotSets":
            if self.live_by_display is not None:
                return {"data": [{"id": f"SET-{display}", "attributes": {"screenshotDisplayType": display}}
                                 for display in self.live_by_display]}
            if self.live_shots is None:
                return {"data": []}
            return {"data": [{"id": f"SET-{display}", "attributes": {"screenshotDisplayType": display}}
                             for _, display, _ in asc_release.SCREENSHOT_SETS]}
        if path.startswith("/appScreenshotSets/") and path.endswith("/appScreenshots"):
            # Every live set holds live_shots; sets created by this run start empty.
            live = self.live_shots if path.startswith("/appScreenshotSets/SET-") else []
            if self.live_by_display is not None and path.startswith("/appScreenshotSets/SET-"):
                live = self.live_by_display[path.split("/")[2].removeprefix("SET-")]
            return {"data": [{"id": f"SHOT{i}", "attributes": shot} for i, shot in enumerate(live or [])]}
        if path == f"/apps/{APP}/subscriptionGroups?limit=200":
            return {"data": []}
        if path == f"/apps/{APP}/inAppPurchasesV2?limit=200":
            return {"data": self.iaps}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        if path == "/appScreenshotSets":
            display = body["data"]["attributes"]["screenshotDisplayType"]
            self.created_sets.append(display)
            return {"data": {"id": f"NEW-{display}"}}
        return {"data": {"id": "ITEM"}}

    def patch(self, path: str, body: dict) -> dict:
        self.calls.append(("PATCH", path))
        return {"data": body["data"]}

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path))
        if method == "PATCH" and path == "/appStoreVersions/V1/relationships/build":
            self.attached = body["data"]["id"]
        return None


def writes(fake: FakeAsc) -> list[tuple[str, str]]:
    return [call for call in fake.calls if call[0] != "GET"]


class PreflightClient:
    """Read-only App Store Connect fake: any write attempt fails the test."""

    def __init__(self, versions: list[dict], *, localization: bool = True) -> None:
        self.versions = versions
        self.localization = localization
        self.paths: list[str] = []

    def get(self, path: str) -> dict:
        self.paths.append(path)
        if path.startswith("/apps?"):
            return {"data": [{"id": APP, "attributes": {"bundleId": ios_release.BUNDLE_ID}}]}
        if path.startswith(f"/apps/{APP}/appStoreVersions"):
            return {"data": self.versions}
        if path.startswith("/appStoreVersions/V1/appStoreVersionLocalizations"):
            return {"data": [{"id": "LOC1"}] if self.localization else []}
        # A READY_FOR_REVIEW version sits in its draft, as in App Store Connect.
        if path.startswith(f"/apps/{APP}/reviewSubmissions"):
            return {"data": [{"id": "D1"}]}
        if path.startswith("/reviewSubmissions/D1/items"):
            return {"data": [{"id": "I1", "relationships": {
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": "V1"}}}}]}
        raise AssertionError(f"unexpected GET {path}")


def asc_version(state: str, version: str = VERSION) -> dict:
    return {"id": "V1", "attributes": {"versionString": version, "appStoreState": state}}


class VersionCreationClient:
    """App Store Connect stand-in for `asc_release.py --create-version` (no network).

    Only the app lookup, the IOS version listing and POST /appStoreVersions are
    expected; any other call fails the test.
    """

    def __init__(self, *, versions: list[tuple[str, str]] | None = None,
                 app_id: str = asc_release.ASC_APP_ID, listed_after_create: bool = True) -> None:
        # The app's IOS App Store versions as (versionString, appStoreState).
        self.versions = [("7.1.2", "READY_FOR_SALE")] if versions is None else list(versions)
        self.app_id = app_id
        # False: the created version does not show up in the listing yet.
        self.listed_after_create = listed_after_create
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if path.startswith("/apps?"):
            return {"data": [{"id": self.app_id, "attributes": {"bundleId": asc_release.DEFAULT_BUNDLE_ID}}]}
        if path.startswith(f"/apps/{self.app_id}/appStoreVersions"):
            return {"data": [{"id": f"V{i}", "attributes": {"versionString": number, "appStoreState": state}}
                             for i, (number, state) in enumerate(self.versions)]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path))
        self.bodies.append(body)
        if path != "/appStoreVersions":
            raise AssertionError(f"unexpected POST {path}")
        if self.listed_after_create:
            self.versions.append((body["data"]["attributes"]["versionString"], "PREPARE_FOR_SUBMISSION"))
        return {"data": {"id": "NEW"}}

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


@functools.lru_cache(maxsize=None)
def creative_png(width: int, height: int, colour_type: int = 2) -> bytes:
    return make_png(width, height, colour_type)


def jpeg(width: int, height: int) -> bytes:
    """A JPEG header with a baseline start-of-frame segment (enough to read its size)."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x01\x11\x00" * 3
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


class CreativeFake(FakeAsc):
    """FakeAsc plus the Asset Library, image placements and any number of review drafts."""

    def __init__(self, *, library: list[dict] | None = None, placements: dict[str, dict] | None = None,
                 drafts: list[str] | None = None, draft_items: dict[str, list[dict]] | None = None,
                 processing: dict[str, list[str]] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.library = [json_copy(image) for image in library or []]
        # placement id -> {"type", "image", "loc", "group", "state"}
        self.placements = {pid: dict(p) for pid, p in (placements or {}).items()}
        self.drafts = list(drafts or [])
        self.items = {draft: list(rows) for draft, rows in (draft_items or {}).items()}
        # image id -> the states successive GETs of the image report while Apple processes it
        self.processing = {image_id: list(states) for image_id, states in (processing or {}).items()}
        self.submitted: list[str] = []
        self.placed: list[dict] = []
        self.counter = 0

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def image(self, image_id: str) -> dict:
        return next(image for image in self.library if image["id"] == image_id)

    def get(self, path: str) -> dict:
        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)
        if path == f"/apps/{APP}/assetLibrary":
            self.calls.append(("GET", path))
            return {"data": {"type": "appAssetLibraries", "id": "LIB"}}
        if parsed.path == "/appAssetLibraries/LIB/images":
            self.calls.append(("GET", path))
            assert query.get("filter[category]") == ["CREATIVE_ASSETS"], path
            return {"data": [json_copy(image) for image in self.library]}
        if parsed.path.startswith("/appAssetLibraryImages/") and parsed.path.endswith("/relationships/placements"):
            self.calls.append(("GET", path))
            image_id = parsed.path.split("/")[2]
            return {"data": [{"type": "appAssetLibraryPlacements", "id": pid}
                             for pid, p in self.placements.items() if p["image"] == image_id]}
        if parsed.path.startswith("/appAssetLibraryImages/"):
            self.calls.append(("GET", path))
            image = self.image(parsed.path.split("/")[2])
            states = self.processing.get(image["id"])
            if states:
                image["attributes"]["state"] = states.pop(0)
                if image["attributes"]["state"] == "PREPARE_FOR_SUBMISSION":
                    image["attributes"]["imageAsset"] = {"width": 1, "height": 1}
            return {"data": json_copy(image)}
        if parsed.path == "/appStoreVersionLocalizations/LOC1/placements":
            self.calls.append(("GET", path))
            wanted = (query.get("filter[placementType]") or [""])[0]
            return {"data": [{"type": "appAssetLibraryPlacements", "id": pid,
                              "attributes": {"placementType": p["type"], "placementGroup": p.get("group", "DEFAULT_PROFILE"),
                                             "state": p.get("state", "ACTIVE")}}
                             for pid, p in self.placements.items() if p["loc"] == "LOC1" and p["type"] == wanted]}
        if path.startswith(f"/apps/{APP}/reviewSubmissions"):
            self.calls.append(("GET", path))
            assert query.get("filter[state]") == ["READY_FOR_REVIEW"], path
            return {"data": [{"type": "reviewSubmissions", "id": draft} for draft in self.drafts]}
        if parsed.path.startswith("/reviewSubmissions/") and parsed.path.endswith("/items"):
            self.calls.append(("GET", path))
            assert "appAssetLibraryImage" in (query.get("include") or [""])[0], path
            return {"data": [json_copy(item) for item in self.items.get(parsed.path.split("/")[2], [])]}
        return super().get(path)

    def post(self, path: str, body: dict) -> dict:
        data = body["data"]
        if path == "/appAssetLibraryImages":
            self.calls.append(("POST", path))
            image_id = self.next_id("IMG")
            attributes = data["attributes"]
            self.library.append({"id": image_id, "attributes": {**attributes, "state": "AWAITING_UPLOAD",
                                                                 "createdDate": "2026-10-21"}})
            ops = [{"url": f"https://upload.example/{image_id}", "offset": 0, "length": attributes["fileSize"],
                    "method": "PUT", "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}]
            return {"data": {"type": "appAssetLibraryImages", "id": image_id,
                             "attributes": {"state": "AWAITING_UPLOAD", "uploadOperations": ops}}}
        if path == "/reviewSubmissions":
            self.calls.append(("POST", path))
            draft = self.next_id("DRAFT")
            self.drafts.append(draft)
            self.items[draft] = []
            return {"data": {"type": "reviewSubmissions", "id": draft}}
        if path == "/reviewSubmissionItems":
            self.calls.append(("POST", path))
            rels = data["relationships"]
            draft = rels["reviewSubmission"]["data"]["id"]
            (name, related), = [(k, v) for k, v in rels.items() if k != "reviewSubmission"]
            self.items.setdefault(draft, []).append(
                {"id": self.next_id("ITEM"), "relationships": {name: {"data": dict(related["data"])}}})
            if name == "appAssetLibraryImage":
                self.image(related["data"]["id"])["attributes"]["state"] = "READY_FOR_REVIEW"
            return {"data": {"type": "reviewSubmissionItems", "id": "ITEM"}}
        if path == "/appAssetLibraryPlacements":
            self.calls.append(("POST", path))
            self.placed.append(body)
            rels = data["relationships"]
            swap = (rels.get("placementToSwapOut") or {}).get("data")
            if swap:
                del self.placements[swap["id"]]
            self.placements[self.next_id("PL")] = {
                "type": data["attributes"]["placementType"], "image": rels["image"]["data"]["id"],
                "loc": rels["appStoreVersionLocalization"]["data"]["id"],
                "group": data["attributes"]["placementGroup"]}
            return {"data": {"type": "appAssetLibraryPlacements", "id": "PL"}}
        return super().post(path, body)

    def patch(self, path: str, body: dict) -> dict:
        if path.startswith("/appAssetLibraryImages/"):
            self.calls.append(("PATCH", path))
            image = self.image(path.split("/")[2])
            assert body["data"]["attributes"] == {"uploaded": True}
            image["attributes"]["state"] = "UPLOAD_COMPLETE"
            self.processing.setdefault(image["id"], ["UPLOAD_COMPLETE", "PREPARE_FOR_SUBMISSION"])
            return {"data": body["data"]}
        return super().patch(path, body)

    def request(self, method: str, path: str, body: dict | None = None):
        if method == "PATCH" and path.startswith("/reviewSubmissions/"):
            self.calls.append((method, path))
            draft = path.split("/")[2]
            assert body is not None and body["data"]["attributes"] == {"submitted": True}
            self.submitted.append(draft)
            self.drafts.remove(draft)
            for item in self.items.get(draft, []):
                image_id = related(item, "appAssetLibraryImage")
                if image_id:
                    self.image(image_id)["attributes"]["state"] = "WAITING_FOR_REVIEW"
            return None
        return super().request(method, path, body)

    def upload_bytes(self, url: str, data: bytes, headers: dict, *, method: str = "PUT") -> None:
        self.calls.append(("UPLOAD", url))
        image = self.image(url.rsplit("/", 1)[1])
        assert len(data) == image["attributes"]["fileSize"] and headers == {"Content-Type": "image/png"}


def json_copy(value):
    return json.loads(json.dumps(value))


def related(item: dict, name: str):
    return ((item.get("relationships") or {}).get(name) or {}).get("data", {}).get("id")


def image_item(image_id: str) -> dict:
    return {"id": f"I-{image_id}", "relationships": {"appAssetLibraryImage": {"data": {"type": "appAssetLibraryImages", "id": image_id}}}}


def version_item(version_id: str = "V1") -> dict:
    return {"id": f"I-{version_id}", "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}
