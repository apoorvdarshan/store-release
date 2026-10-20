"""ios plan --version V --work-dir D: what a tag run would change in App Store Connect, read-only.

The plan runs the release's own code (version creation, the version preflight,
build numbering and the store step) against a PlanClient. GETs go to App Store
Connect through a ReadOnlyClient; every write is recorded, answered from an
overlay so the next reads see its effect, and never sent. Report-only: it exits
0 when the plan was computed (CLEAN or with problems, each also a warning
annotation) and 1 only when no plan could be computed.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import sys
import traceback
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable, Mapping

from store_release import __version__
from store_release.ios import asc
from store_release.ios import release as ios_release

PLAN_VERSION_ID = "plan-version-1"
PLAN_BUILD_ID = "plan-build"
SWITCH_KEYS = ("create_version", "upload_build", "listing", "whats_new", "screenshots", "creative", "submit",
               "public_release")


def _route(path: str) -> tuple[str, dict[str, list[str]]]:
    if path.startswith(asc.ASC_BASE):
        path = path[len(asc.ASC_BASE):]
    parsed = urllib.parse.urlparse(path)
    return parsed.path, urllib.parse.parse_qs(parsed.query)


def _length(value: Any) -> int:
    return len(value) if isinstance(value, str) else 0


class PlanClient:
    """An AscClient stand-in: GETs go to the read-only transport, writes are recorded, never sent."""

    def __init__(self, transport: Any, *, version_string: str = "", build_number: str = "") -> None:
        self.transport = transport
        self.version_string = version_string
        self.build_number = build_number
        self.writes: list[dict[str, Any]] = []
        self.sent: list[str] = []
        self.counter = 0
        self.approximate = False
        self.newest_version_id: str | None = None
        self.virtual_versions: list[dict] = []
        self.localization_patch: dict[str, dict] = {}
        self.live_localization: dict[str, dict] = {}
        self.virtual_sets: dict[str, list[dict]] = {}
        self.set_display: dict[str, str] = {}
        self.set_localization: dict[str, str] = {}
        self.virtual_shots: dict[str, list[dict]] = {}
        self.shot_names: dict[str, str] = {}
        self.deleted_shots: set[str] = set()
        self.virtual_images: dict[str, dict] = {}
        self.image_library: dict[str, str] = {}
        self.image_state: dict[str, str] = {}
        self.virtual_drafts: list[str] = []
        self.submitted_drafts: set[str] = set()
        self.virtual_items: dict[str, list[dict]] = {}
        self.selected_build: dict[str, str] = {}
        self.virtual_placements: dict[str, list[dict]] = {}
        self.removed_placements: set[str] = set()
        self.image_placements: dict[str, list[str]] = {}
        self.seen_versions: dict[str, str] = {}

    # --- reads -------------------------------------------------------------------

    def get(self, path: str) -> dict[str, Any]:
        route, query = _route(path)
        parts = route.strip("/").split("/")
        if route == f"/builds/{PLAN_BUILD_ID}":
            return self._planned_build()
        if len(parts) == 3 and parts[0] == "appStoreVersions" and parts[2] == "build" and parts[1] in self.selected_build:
            return {"data": {"type": "builds", "id": self.selected_build[parts[1]]}}
        if len(parts) == 3 and parts[0] == "appScreenshotSets" and parts[2] == "appScreenshots" and \
                parts[1] in self.virtual_shots and parts[1].startswith("plan-"):
            return {"data": [copy.deepcopy(s) for s in self.virtual_shots[parts[1]] if s["id"] not in self.deleted_shots]}
        if len(parts) == 2 and parts[0] == "appAssetLibraryImages" and parts[1] in self.virtual_images:
            return {"data": copy.deepcopy(self.virtual_images[parts[1]])}
        if len(parts) == 3 and parts[0] == "reviewSubmissions" and parts[2] == "items" and parts[1] in self.virtual_drafts:
            return {"data": copy.deepcopy(self.virtual_items.get(parts[1], []))}
        if len(parts) == 3 and parts[0] == "appStoreVersionLocalizations" and parts[1].startswith("plan-"):
            return {"data": []}
        forward = path
        if PLAN_VERSION_ID in route:
            if self.newest_version_id is None:
                return {"data": []}
            # A new version starts as a copy of the newest existing one.
            forward = path.replace(PLAN_VERSION_ID, self.newest_version_id)
            self.approximate = True
        payload = self.transport.get(forward)
        self.sent.append(forward)
        return self._overlay(route, query, parts, copy.deepcopy(payload))

    def _planned_build(self) -> dict[str, Any]:
        return {"data": {"type": "builds", "id": PLAN_BUILD_ID,
                         "attributes": {"version": self.build_number, "processingState": "VALID", "expired": False},
                         "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": "plan-pre"}}}},
                "included": [{"type": "preReleaseVersions", "id": "plan-pre",
                              "attributes": {"version": self.version_string, "platform": "IOS"}}]}

    def _overlay(self, route: str, query: dict, parts: list[str], payload: dict) -> dict:
        data = payload.get("data")
        last_page = not (payload.get("links") or {}).get("next")
        rows = data if isinstance(data, list) else []
        if len(parts) == 3 and parts[0] == "apps" and parts[2] == "appStoreVersions":
            real = [row for row in rows if row.get("id") != PLAN_VERSION_ID]
            for row in real:
                attributes = row.get("attributes") or {}
                self.seen_versions[attributes.get("versionString", "?")] = attributes.get("appStoreState") or "?"
            keyed = [(asc.version_key((row.get("attributes") or {}).get("versionString", "")), row["id"]) for row in real
                     if asc.version_key((row.get("attributes") or {}).get("versionString", "")) is not None]
            if keyed:
                best = max(keyed)
                if self.newest_version_id is None or best[0] is not None:
                    self.newest_version_id = best[1]
            if last_page:
                rows.extend(copy.deepcopy(self.virtual_versions))
        elif len(parts) == 3 and parts[0] == "appStoreVersions" and parts[2] == "appStoreVersionLocalizations":
            for row in rows:
                self.live_localization.setdefault(row["id"], dict(row.get("attributes") or {}))
                row.setdefault("attributes", {}).update(self.localization_patch.get(row["id"], {}))
        elif len(parts) == 3 and parts[0] == "appStoreVersionLocalizations" and parts[2] == "appScreenshotSets":
            for row in rows:
                self.set_display[row["id"]] = (row.get("attributes") or {}).get("screenshotDisplayType", "?")
                self.set_localization[row["id"]] = parts[1]
            if last_page:
                rows.extend(copy.deepcopy(self.virtual_sets.get(parts[1], [])))
        elif len(parts) == 3 and parts[0] == "appScreenshotSets" and parts[2] == "appScreenshots":
            for row in rows:
                self.shot_names[row["id"]] = (row.get("attributes") or {}).get("fileName", "?")
            rows[:] = [row for row in rows if row.get("id") not in self.deleted_shots]
            if last_page:
                rows.extend(copy.deepcopy([s for s in self.virtual_shots.get(parts[1], [])
                                           if s["id"] not in self.deleted_shots]))
        elif len(parts) == 3 and parts[0] == "appAssetLibraries" and parts[2] == "images":
            for row in rows:
                self._image_state(row)
            if last_page:
                rows.extend(copy.deepcopy([image for image_id, image in self.virtual_images.items()
                                           if self.image_library.get(image_id) == parts[1]]))
        elif len(parts) == 2 and parts[0] == "appAssetLibraryImages" and isinstance(data, dict):
            self._image_state(data)
        elif len(parts) == 3 and parts[0] == "apps" and parts[2] == "reviewSubmissions":
            rows[:] = [row for row in rows if row.get("id") not in self.submitted_drafts]
            if last_page and query.get("filter[state]") == ["READY_FOR_REVIEW"]:
                rows.extend({"type": "reviewSubmissions", "id": draft} for draft in self.virtual_drafts
                            if draft not in self.submitted_drafts)
        elif len(parts) == 3 and parts[0] == "reviewSubmissions" and parts[2] == "items":
            if last_page:
                rows.extend(copy.deepcopy(self.virtual_items.get(parts[1], [])))
        elif len(parts) == 3 and parts[0] == "appStoreVersionLocalizations" and parts[2] == "placements":
            wanted = (query.get("filter[placementType]") or [None])[0]
            rows[:] = [row for row in rows if row.get("id") not in self.removed_placements]
            if last_page:
                rows.extend(copy.deepcopy([row for row in self.virtual_placements.get(parts[1], [])
                                           if wanted is None or row["attributes"]["placementType"] == wanted]))
        elif len(parts) == 4 and parts[0] == "appAssetLibraryImages" and parts[3] == "placements":
            rows[:] = [row for row in rows if row.get("id") not in self.removed_placements]
            if last_page:
                rows.extend({"type": "appAssetLibraryPlacements", "id": pid}
                            for pid in self.image_placements.get(parts[1], []))
        return payload

    def _image_state(self, row: dict) -> None:
        state = self.image_state.get(row.get("id"))
        if state:
            row.setdefault("attributes", {})["state"] = state

    # --- writes --------------------------------------------------------------------

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("POST", path, body)

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("PATCH", path, body)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any] | None:
        if method.upper() == "GET":
            return self.get(path)
        return self._write(method.upper(), path, body)

    def upload_bytes(self, url: str, data: bytes, headers: dict[str, str], *, method: str = "PUT") -> None:
        if not url.startswith("plan://"):
            raise asc.AscHttpError(0, f"store-plan refused an upload to {url.split('?')[0]}")
        self._record("UPLOAD", url, f"send {len(data)} bytes", "", None)

    def _next(self, prefix: str) -> str:
        self.counter += 1
        return f"plan-{prefix}-{self.counter}"

    def _record(self, method: str, path: str, purpose: str, summary: str, body: Any, *, kind: str = "write") -> None:
        self.writes.append({"method": method, "path": path, "purpose": purpose, "summary": summary, "kind": kind,
                            "body": body})

    def _write(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any] | None:
        route, _ = _route(path)
        parts = route.strip("/").split("/")
        data = (body or {}).get("data") or {}
        attributes = data.get("attributes") or {}
        relationships = data.get("relationships") or {}

        def rel(name: str) -> str | None:
            return ((relationships.get(name) or {}).get("data") or {}).get("id")

        if method == "POST" and route == "/appStoreVersions":
            row = {"type": "appStoreVersions", "id": PLAN_VERSION_ID,
                   "attributes": {"versionString": attributes.get("versionString"), "platform": attributes.get("platform"),
                                  "appStoreState": "PREPARE_FOR_SUBMISSION"}}
            self.virtual_versions.append(row)
            self._record(method, route, "create the App Store version",
                         f"{{platform: {attributes.get('platform')}, versionString: {attributes.get('versionString')}}}",
                         body)
            return {"data": copy.deepcopy(row)}
        if method == "PATCH" and len(parts) == 2 and parts[0] == "appStoreVersionLocalizations":
            live = self.live_localization.get(parts[1], {})
            fields = ", ".join(f"{field} {_length(live.get(field))} → {_length(value)} chars"
                               for field, value in attributes.items())
            self.localization_patch.setdefault(parts[1], {}).update(attributes)
            self._record(method, route, "update the version localization", fields, body)
            return {"data": data}
        if method == "POST" and route == "/appScreenshotSets":
            set_id = self._next("set")
            loc = rel("appStoreVersionLocalization") or "?"
            display = attributes.get("screenshotDisplayType", "?")
            row = {"type": "appScreenshotSets", "id": set_id, "attributes": {"screenshotDisplayType": display}}
            self.virtual_sets.setdefault(loc, []).append(row)
            self.set_display[set_id] = display
            self.virtual_shots.setdefault(set_id, [])
            self._record(method, route, f"create the {display} screenshot set", "", body)
            return {"data": copy.deepcopy(row)}
        if method == "POST" and route == "/appScreenshots":
            shot_id = self._next("shot")
            set_id = rel("appScreenshotSet") or "?"
            row = {"type": "appScreenshots", "id": shot_id,
                   "attributes": {"fileName": attributes.get("fileName"), "fileSize": attributes.get("fileSize"),
                                  "uploadOperations": [{"method": "PUT", "url": f"plan://upload/{shot_id}", "offset": 0,
                                                        "length": attributes.get("fileSize") or 0,
                                                        "requestHeaders": []}]}}
            self.virtual_shots.setdefault(set_id, []).append(row)
            self.shot_names[shot_id] = attributes.get("fileName", "?")
            self._record(method, route, f"upload screenshot to {self.set_display.get(set_id, set_id)}",
                         f"{attributes.get('fileName')} ({attributes.get('fileSize')} bytes)", body)
            return {"data": copy.deepcopy(row)}
        if method == "PATCH" and len(parts) == 2 and parts[0] == "appScreenshots":
            self._record(method, route, f"commit screenshot {self.shot_names.get(parts[1], parts[1])}",
                         f"md5 {attributes.get('sourceFileChecksum')}", body)
            return {"data": data}
        if method == "DELETE" and len(parts) == 2 and parts[0] == "appScreenshots":
            self.deleted_shots.add(parts[1])
            self._record(method, route, f"delete live screenshot {self.shot_names.get(parts[1], parts[1])}", "", body)
            return None
        if method == "POST" and route == "/appAssetLibraryImages":
            image_id = self._next("image")
            library = rel("assetLibrary") or "?"
            row = {"type": "appAssetLibraryImages", "id": image_id,
                   "attributes": {"fileName": attributes.get("fileName"), "fileSize": attributes.get("fileSize"),
                                  "category": attributes.get("category"), "state": "AWAITING_UPLOAD",
                                  "createdDate": "9999-12-31",
                                  "uploadOperations": [{"method": "PUT", "url": f"plan://upload/{image_id}", "offset": 0,
                                                        "length": attributes.get("fileSize") or 0,
                                                        "requestHeaders": []}]}}
            self.virtual_images[image_id] = row
            self.image_library[image_id] = library
            self._record(method, route, "upload a creative image to the Asset Library",
                         f"{attributes.get('fileName')} ({attributes.get('fileSize')} bytes)", body)
            return {"data": copy.deepcopy(row)}
        if method == "PATCH" and len(parts) == 2 and parts[0] == "appAssetLibraryImages":
            image = self.virtual_images.get(parts[1])
            if image is not None:
                image["attributes"]["state"] = "PREPARE_FOR_SUBMISSION"
                image["attributes"]["imageAsset"] = {"width": 1, "height": 1}
            self._record(method, route, "commit the creative image upload", "", body)
            return {"data": data}
        if method == "POST" and route == "/reviewSubmissions":
            draft = self._next("draft")
            self.virtual_drafts.append(draft)
            self.virtual_items[draft] = []
            self._record(method, route, "create a draft review submission", f"would create {draft}", body)
            return {"data": {"type": "reviewSubmissions", "id": draft}}
        if method == "POST" and route == "/reviewSubmissionItems":
            draft = rel("reviewSubmission") or "?"
            name, related = next(((key, value) for key, value in relationships.items() if key != "reviewSubmission"),
                                 ("?", {}))
            item = {"type": "reviewSubmissionItems", "id": self._next("item"),
                    "relationships": {name: {"data": dict((related or {}).get("data") or {})}}}
            self.virtual_items.setdefault(draft, []).append(item)
            target = ((related or {}).get("data") or {}).get("id")
            if name == "appAssetLibraryImage" and target:
                self._set_image_state(target, "READY_FOR_REVIEW")
            label = {"appStoreVersion": "the App Store version", "subscriptionVersion": "subscription version",
                     "inAppPurchaseVersion": "in-app purchase version",
                     "appAssetLibraryImage": "creative image"}.get(name, name)
            self._record(method, route, f"add {label} {target} to review submission {draft}", "", body)
            return {"data": copy.deepcopy(item)}
        if method == "PATCH" and len(parts) == 2 and parts[0] == "reviewSubmissions":
            self.submitted_drafts.add(parts[1])
            for item in self.virtual_items.get(parts[1], []):
                image = ((item.get("relationships") or {}).get("appAssetLibraryImage") or {}).get("data") or {}
                if image.get("id"):
                    self._set_image_state(image["id"], "WAITING_FOR_REVIEW")
            self._record(method, route, f"submit review submission {parts[1]}", "submitted: true", body)
            return None
        if method == "PATCH" and len(parts) == 4 and parts[0] == "appStoreVersions" and parts[2:] == ["relationships", "build"]:
            self.selected_build[parts[1]] = data.get("id", "")
            self._record(method, route, f"select build {data.get('id')} for the version", "", body)
            return None
        if method == "PATCH" and len(parts) == 2 and parts[0] == "appStoreVersions":
            self._record(method, route, f"set releaseType {attributes.get('releaseType')}", "", body)
            return {"data": data}
        if method == "POST" and route == "/inAppPurchaseSubmissions":
            self._record(method, route, f"submit in-app purchase {rel('inAppPurchaseV2')} for review", "", body)
            return {"data": {"type": "inAppPurchaseSubmissions", "id": self._next("iap")}}
        if method == "POST" and route == "/appAssetLibraryPlacements":
            placement = self._next("placement")
            loc = rel("appStoreVersionLocalization") or "?"
            image = rel("image") or "?"
            swap = rel("placementToSwapOut")
            row = {"type": "appAssetLibraryPlacements", "id": placement,
                   "attributes": {"placementType": attributes.get("placementType"),
                                  "placementGroup": attributes.get("placementGroup"), "state": "ACTIVE"}}
            self.virtual_placements.setdefault(loc, []).append(row)
            self.image_placements.setdefault(image, []).append(placement)
            if swap:
                self.removed_placements.add(swap)
            self._record(method, route, f"place image {image} as {attributes.get('placementType')}",
                         f"replacing placement {swap}" if swap else "", body)
            return {"data": copy.deepcopy(row)}
        self._record(method, route, "unrecognised write", "", body, kind="unrecognised")
        return {"data": {"id": self._next("write"), "type": data.get("type")}}

    def _set_image_state(self, image_id: str, state: str) -> None:
        if image_id in self.virtual_images:
            self.virtual_images[image_id]["attributes"]["state"] = state
        else:
            self.image_state[image_id] = state


# --- the plan ----------------------------------------------------------------------


class PlanAbort(Exception):
    """The plan cannot be computed (missing credentials, an auth error, a refused write)."""


def last_error(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("error: "):
            return line[len("error: "):]
    return lines[-1] if lines else ""


def attempt(fn: Callable[[], Any]) -> tuple[bool, Any, str, str]:
    """(ok, value, stdout, problem) for one release step; auth errors and refusals abort the plan."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            value = fn()
        return True, value, out.getvalue(), ""
    except asc.AscHttpError as exc:
        if exc.status in (0, 401, 403):
            raise PlanAbort(last_error(err.getvalue()) or str(exc.code)) from None
        return False, None, out.getvalue(), last_error(err.getvalue()) or str(exc.code)
    except ios_release.ReleaseError as exc:
        return False, None, out.getvalue(), str(exc)
    except SystemExit as exc:
        detail = last_error(err.getvalue()) or (str(exc.code) if not isinstance(exc.code, int) else "")
        return False, None, out.getvalue(), detail or f"exited {exc.code}"


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.problems: list[str] = []

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def problem(self, message: str) -> int:
        self.problems.append(message)
        return len(self.problems)

    def blocked(self, number: int) -> str:
        return f"not planned (blocked by problem {number})"


def switch_summary(planned: Mapping[str, Any]) -> str:
    master = "on" if planned.get("master") else "off"
    values = " ".join(f"{key}={'on' if planned.get(key) else 'off'}" for key in SWITCH_KEYS)
    return f"master={master} {values}"


def run_plan(app: Any, *, version: str, work_dir: Path, environ: Mapping[str, str] | None = None,
             transport: Any = None, gh_runner: Callable | None = None) -> tuple[int, dict]:
    from store_release import github, screenshots  # noqa: PLC0415
    from store_release.text import listing, whats_new  # noqa: PLC0415

    env = dict(os.environ if environ is None else environ)
    config = app.config
    ios = config.ios
    tag = ios.tag_prefix + version
    work_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    sha = env.get("TOOL_SHA") or env.get("GITHUB_WORKFLOW_SHA", "")
    report.line(f"store-plan ios: {config.app.name} {version} (tag {tag}), store-release v{__version__}"
                + (f" ({sha})" if sha else ""))
    result: dict[str, Any] = {"platform": "ios", "version": version, "tag": tag, "phases": [], "writes": [],
                              "problems": report.problems}

    def phase(name: str, text: str) -> None:
        result["phases"].append({"name": name, "outcome": text})
        report.line(f"{len(result['phases'])}. {name}: {text}")

    try:
        planned = ios_release.resolve_gates(env, event="push", ref_name=tag, marketing_version=version)
        ios_release.check_switch_tables(planned)
    except ios_release.ReleaseError as exc:
        number = report.problem(str(exc))
        phase("switches", report.blocked(number))
        return finish(report, result, work_dir, env)
    result["switches"] = {key: planned[key] for key in ("master", *SWITCH_KEYS)}
    report.line(f"switches a tag run would use: {switch_summary(planned)}")
    for note in planned.get("notes", []):
        report.line(f"  note: {note}")

    build_number = ""
    if not planned["master"]:
        phase("App Store Connect", "a tag run would not touch App Store Connect (IOS_RELEASE_AUTOMATION is off)")
    else:
        build_number = plan_store(app, planned, version, work_dir, env, report, phase, result, transport,
                                  listing=listing, whats_new=whats_new, screenshots=screenshots)
    plan_github_release(app, planned, version, tag, build_number, env, report, phase, result, gh_runner or github.run_gh)
    return finish(report, result, work_dir, env)


def plan_store(app, planned, version, work_dir, env, report, phase, result, transport, *, listing, whats_new,
               screenshots) -> str:
    meta = work_dir / "store-meta"
    shots = work_dir / "screenshots"
    local_problem: int | None = None
    if planned["listing"]:
        ok, _, _, problem = attempt(lambda: _module_main(listing, ["--platform", "ios", "--out", str(meta)]))
        if not ok:
            local_problem = local_problem or report.problem(f"listing text: {problem}")
    if planned["whats_new"]:
        tag = app.config.ios.tag_prefix + version
        ok, _, _, problem = attempt(lambda: _module_main(whats_new, ["--platform", "ios", "--tag", tag, "--out", str(meta)]))
        if not ok:
            local_problem = local_problem or report.problem(f"What's New: {problem}")
    if planned["screenshots"]:
        def stage() -> None:
            saved = os.environ.get("STORE_UPLOAD_SCREENSHOTS")
            os.environ["STORE_UPLOAD_SCREENSHOTS"] = "true"
            try:
                screenshots.restage(shots)
                screenshots.stage(dry_run=False, platform="ios")
            finally:
                if saved is None:
                    os.environ.pop("STORE_UPLOAD_SCREENSHOTS", None)
                else:
                    os.environ["STORE_UPLOAD_SCREENSHOTS"] = saved

        ok, _, _, problem = attempt(stage)
        if not ok:
            local_problem = local_problem or report.problem(f"screenshots: {problem}")
    if planned["creative"]:
        ok, _, _, problem = attempt(lambda: asc.creative_sources(asc.DEFAULT_CREATIVE_DIR))
        if not ok:
            local_problem = local_problem or report.problem(f"header / search results images: {problem}")

    if transport is None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            try:
                transport = asc.client_from_environment(read_only=True)
            except SystemExit:
                raise PlanAbort(last_error(err.getvalue()) or "missing App Store Connect credentials") from None
    client = PlanClient(transport, version_string=version)
    result["client"] = client

    blocked: int | None = None
    if planned["create_version"]:
        ok, _, out, problem = attempt(lambda: asc.ensure_ios_app_store_version(
            client, bundle_id=asc.DEFAULT_BUNDLE_ID, expected_app_id=asc.ASC_APP_ID, version_string=version))
        if not ok:
            blocked = report.problem(f"create-version: {problem}")
            phase("create-version", f"refused: {problem}")
        elif any(w["method"] == "POST" and w["path"] == "/appStoreVersions" for w in client.writes):
            phase("create-version", f"App Store version {version} is missing → WOULD CREATE (POST /appStoreVersions)")
            report.line(f"   refusal checks passed: {_versions_seen(client)}; {version} is newer than every version")
        else:
            phase("create-version", out.strip().splitlines()[-1] if out.strip() else f"{version} exists")
    if planned["asc"]:
        if blocked:
            phase("preflight", report.blocked(blocked))
        else:
            metadata = any(planned[key] for key in ("listing", "whats_new", "screenshots", "creative"))
            ok, state, _, problem = attempt(lambda: ios_release.version_preflight(
                client, asc, version=version, metadata=metadata, submit=planned["submit"],
                creative=planned["creative"]))
            if ok:
                approximate = " (approximate: new version)" if client.approximate else ""
                phase("preflight", f"App Store version {version} is {state}: it can take this release{approximate}")
            else:
                blocked = report.problem(f"preflight: {problem}")
                phase("preflight", f"refused: {problem}")
    build_number = ""
    if planned["upload_build"]:
        ok, value, _, problem = attempt(lambda: _next_build_number(client))
        if ok:
            build_number, project, highest = value
            client.build_number = build_number
            phase("build", f"next build number {build_number} (project {project}, highest uploaded {highest}); "
                           "xcodebuild uploads the binary")
        else:
            number = report.problem(f"build number: {problem}")
            phase("build", f"refused: {problem}")
            blocked = blocked or number
    if not planned["asc"]:
        phase("store step", "not planned (no listing/What's New/screenshot/creative/review switch on)")
    elif blocked or local_problem:
        phase("store step", report.blocked(blocked or local_problem))
    else:
        args = argparse.Namespace(
            version=version, build_id=PLAN_BUILD_ID if planned["submit"] else None,
            build_number=build_number if planned["submit"] else None, metadata_dir=meta,
            screenshots_dir=shots / "ios", creative_dir=asc.DEFAULT_CREATIVE_DIR, locale=asc.LOCALE,
            bundle_id=asc.DEFAULT_BUNDLE_ID)
        ok, _, out, problem = attempt(lambda: asc.store_step(client, args=args, flags=planned))
        result["store_step_log"] = out
        if ok:
            simulated = " (build selection simulated: no build in a validation run)" if planned["submit"] else ""
            approximate = " (approximate: new version)" if client.approximate else ""
            phase("store step", f"{len(client.writes)} write(s) planned{simulated}{approximate}")
            for line in out.splitlines():
                if line.strip():
                    report.line(f"   | {line.rstrip()}")
        else:
            report.problem(f"store step: {problem}")
            phase("store step", f"refused: {problem}")
    return build_number


def _versions_seen(client: PlanClient) -> str:
    if not client.seen_versions:
        return "no earlier version"
    ordered = sorted(client.seen_versions.items(), key=lambda item: asc.version_key(item[0]) or (0,), reverse=True)
    return ", ".join(f"{number} {state}" for number, state in ordered)


def _next_build_number(client: PlanClient) -> tuple[str, str, int]:
    project = ios_release.single_project_value("CURRENT_PROJECT_VERSION")
    if not re.fullmatch(r"[0-9]+", project):
        raise ios_release.ReleaseError(f"CURRENT_PROJECT_VERSION must be a whole number, got {project!r}")
    app_id = asc.find_app(client, ios_release.BUNDLE_ID)
    rows = asc.iter_collection(client, "/builds?" + urllib.parse.urlencode(
        {"filter[app]": app_id, "fields[builds]": "version", "limit": "200"}))
    versions = [(row.get("attributes") or {}).get("version", "") for row in rows]
    numeric = [int(v) for v in versions if re.fullmatch(r"[0-9]+", v or "")]
    return str(ios_release.next_build_number(int(project), versions)), project, max(numeric, default=0)


def _module_main(module, argv: list[str]) -> None:
    saved = sys.argv
    sys.argv = ["store-plan", *argv]
    try:
        module.main()
    finally:
        sys.argv = saved


def plan_github_release(app, planned, version, tag, build_number, env, report, phase, result, gh_runner) -> None:
    from store_release import github  # noqa: PLC0415

    uploaded = build_number if planned.get("master") and planned.get("upload_build") else ""
    try:
        title = github.ios_title(app.config, app.root, version=version, uploaded_build=uploaded)
    except (OSError, ValueError) as exc:
        phase("GitHub Release", f"title not planned ({exc})")
        return
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo:
        phase("GitHub Release", f"{title!r}; Latest not planned (GITHUB_REPOSITORY is not set)")
        return
    try:
        tags = github.release_tags(repo, gh_runner)
        exists = github.release_exists(tag, repo, gh_runner)
    except github.GithubError as exc:
        phase("GitHub Release", f"{title!r}; Latest not planned ({exc})")
        return
    latest = "--latest" if github.is_highest(tag, app.config.ios.tag_prefix, tags) else "--latest=false"
    action = "update the existing release" if exists else "create the release"
    result["github_release"] = {"title": title, "latest": latest, "exists": exists}
    phase("GitHub Release", f"{action} {tag} titled {title!r} with {latest}")


def finish(report: Report, result: dict, work_dir: Path, env: Mapping[str, str]) -> tuple[int, dict]:
    client = result.pop("client", None)
    writes = client.writes if client is not None else []
    result["writes"] = [dict(write) for write in writes]
    if writes:
        report.line("writes a tag run would make, in order:")
        for write in writes:
            summary = f": {write['summary']}" if write["summary"] else ""
            label = " [unrecognised write]" if write["kind"] == "unrecognised" else ""
            report.line(f"  {write['method']} {write['path']}  {write['purpose']}{summary}{label}")
    else:
        report.line("writes a tag run would make: none")
    if report.problems:
        report.line("problems a tag run would hit:")
        for number, problem in enumerate(report.problems, 1):
            report.line(f"  {number}. {problem}")
    else:
        report.line("problems a tag run would hit: none")
    status = "store-plan: CLEAN" if not report.problems else f"store-plan: {len(report.problems)} PROBLEM(S)"
    report.line(status)
    result["status"] = status
    # The status is the last line; every problem is also a warning annotation, printed before it.
    print("\n".join(report.lines[:-1]))
    for problem in report.problems:
        print(f"::warning title=store-plan::{problem}")
    print(report.lines[-1])
    (work_dir / "store-plan.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("## " + report.lines[0] + "\n\n```\n" + "\n".join(report.lines[1:]) + "\n```\n")
    return 0, result


def main(app: Any, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ios plan", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not ios_release.VERSION_PATTERN.fullmatch(args.version):
        print(f"error: --version {args.version!r} must look like 7.2 or 7.2.1", file=sys.stderr)
        return 1
    try:
        code, _ = run_plan(app, version=args.version, work_dir=args.work_dir)
        return code
    except PlanAbort as exc:
        print(f"error: the store plan could not be computed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - report-only: print the traceback and fail the plan job
        traceback.print_exc()
        print("error: the store plan could not be computed (unexpected error above)", file=sys.stderr)
        return 1
