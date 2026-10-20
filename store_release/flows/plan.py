"""The read-only store plan of a ported flow (validation dispatches: ios plan / android plan).

The flow's switches are resolved as a tag run of this version would resolve them (the
same repository variables), then the flow's own read-only checks run against the live
store with a client that refuses every write before it is sent:

  iOS      the catalog and the local files a tag run's prepare checks, the App Store version
           (would IOS_CREATE_VERSION create it?), and the flow's read-only preflight with the
           switches a tag run would have. When the version does not exist yet and
           IOS_CREATE_VERSION would create it, the preflight cannot run (it needs the
           version), so every check that does not need it runs instead: What's New against
           the live version, the products that would have to go to review, the review drafts.
           What only the created version can show is listed as not checked.
  Android  the flow's read-only Play checks a tag run makes before building
           (check-version-code; with a track selected, play-state)

Report-only, like the standard flow's plan: it exits 0 when the plan was computed (CLEAN, or
with the problems a tag run would hit, each also a warning annotation) and 1 only when it
could not be computed. It lists the checks a tag run would fail, not every write the store
step would make: the ported store steps have no write simulation.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Iterator, Mapping

from store_release.flows import commands


class ReadOnlyViolation(AssertionError):
    """A plan tried to change the store."""


class GetOnlyClient:
    """GET requests only: every other method raises before anything is sent."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, path: str) -> dict:
        return self._client.get(path)

    def request(self, method: str, path: str, *args: Any, **kwargs: Any):
        if method.upper() != "GET":
            raise ReadOnlyViolation(f"the store plan is read-only; refused {method} {path}")
        return self._client.request(method, path, *args, **kwargs)

    def post(self, path: str, body: Any) -> dict:
        raise ReadOnlyViolation(f"the store plan is read-only; refused POST {path}")

    def patch(self, path: str, body: Any) -> dict:
        raise ReadOnlyViolation(f"the store plan is read-only; refused PATCH {path}")

    def upload_bytes(self, url: str, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolation("the store plan is read-only; refused an upload")


@contextlib.contextmanager
def read_only(flow: str) -> Iterator[None]:
    """Every App Store Connect client the flow makes in this block is GET-only."""
    asc = commands.module(flow, "asc_release")
    original = asc.client_from_environment
    asc.client_from_environment = lambda *args, **kwargs: GetOnlyClient(original(*args, **kwargs))
    try:
        yield
    finally:
        asc.client_from_environment = original


VERSION_LISTS = {"scowld": "ios_versions", "verceltics": "list_ios_app_store_versions",
                 "abysm": "list_ios_app_store_versions", "pcm-notes": "ios_app_store_versions",
                 "quit-all": "list_ios_app_store_versions"}


def run_step(label: str, fn) -> tuple[bool, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            fn()
            ok = True
        except SystemExit as exc:
            ok = exc.code in (0, None)
        except ReadOnlyViolation as exc:
            print(f"error: {exc}")
            ok = False
        except (OSError, ValueError) as exc:
            # A file the step reads is missing or malformed: the tag run's own step would stop on it too.
            print(f"error: {exc}")
            ok = False
    text = out.getvalue().strip()
    print(f"== {label}: {'passed' if ok else 'a tag run would fail here'}")
    if text:
        print(text)
    return ok, error_message(text) or f"{label} failed"


def error_message(text: str) -> str:
    """The last `error: ...` line of a step's output with the indented lines that continue it."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("error:")]
    if not starts:
        return ""
    message = [lines[starts[-1]].removeprefix("error: ").strip()]
    for line in lines[starts[-1] + 1:]:
        if not line.startswith((" ", "\t")) or not line.strip():
            break
        message.append(line.strip())
    return " ".join(message)


def finish(flow: str, platform: str, problems: list[str], lines: list[str], work_dir: Path,
           env: Mapping[str, str]) -> int:
    status = "store-plan: CLEAN" if not problems else f"store-plan: {len(problems)} PROBLEM(S)"
    print(f"{platform} store plan ({flow} flow):")
    for line in lines:
        print(f"  {line}")
    for number, problem in enumerate(problems, 1):
        print(f"  problem {number}: {problem}")
        print(f"::warning title=store-plan::{problem}")
    print(status)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "store-plan.json").write_text(json.dumps(
        {"flow": flow, "platform": platform, "status": status, "lines": lines, "problems": problems}, indent=2)
        + "\n", encoding="utf-8")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"## {platform} store plan ({flow} flow)\n\n```\n" + "\n".join(lines + problems + [status])
                         + "\n```\n")
    return 0


SWITCH_ENV = {"listing": "UPLOAD_LISTING", "whats_new": "UPLOAD_WHATS_NEW", "screenshots": "UPLOAD_SCREENSHOTS",
              "creative": "UPLOAD_CREATIVE_ASSETS", "submit": "SUBMIT_IOS_REVIEW", "public_release": "PUBLIC_RELEASE"}


def offline_checks(config: Any, flow: str, resolved: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """What a tag run's prepare checks before it reads App Store Connect: the flow's catalog check (as on a
    tag run) and, with an App Store Connect switch on, the local files the enabled switches use."""
    checks: list[tuple[str, Any]] = []
    if config.catalog is not None:
        checks.append(("the catalog check a tag run's prepare makes (offline)",
                       lambda: commands.catalog_check(config, ["--platform", "ios", "--event", "push"])))
    if resolved.get("asc"):
        checks.append(("the local files the enabled switches upload (offline)",
                       lambda: commands.check_inputs(config, flow, [])))
    return checks


def whats_new_checks(flow: str, client: Any, app_id: str, version_id: str) -> list[tuple[str, Any]]:
    """The flow's own check that What's New is not the live version's text, for every locale. version_id is
    the App Store version the release edits ("" while it does not exist yet). Scowld's preflight already
    makes this check for an existing version; PCM Notes' store step makes it, its preflight does not."""
    asc = commands.module(flow, "asc_release")
    if flow == "scowld":
        def scowld() -> None:
            plan = asc.validate_local_inputs(upload_listing=False, upload_whats_new=True, upload_screenshots=False,
                                             metadata_dir=asc.METADATA_DIR, root=asc.ROOT)
            for locale, entry in plan.items():
                asc.refuse_stale_whats_new(client, app_id, version_id, locale, entry["whats_new"])
            print(f"What's New differs from the live version's in {', '.join(plan) or 'no locale'}")
        return [("What's New is not the live version's (every locale)", scowld)]
    if flow == "pcm-notes":
        def pcm_notes() -> None:
            metadata_dir = asc.ROOT / "store" / "metadata"
            locales = asc.listing_locales(metadata_dir)
            for locale in locales:
                text = asc.read_text(metadata_dir / "ios" / locale / asc.WHATS_NEW_FIELD[1])
                if text:
                    asc.check_whats_new_is_new(client, app_id=app_id, version_id=version_id, locale=locale, text=text)
            print(f"What's New is not the live version's in {', '.join(locales) or 'no locale'}")
        return [("What's New is not the live version's (every locale)", pcm_notes)]
    return []


def review_checks(flow: str, client: Any, app_id: str) -> list[tuple[str, Any]]:
    """With IOS_SUBMIT_REVIEW on: the checks of the products and the review drafts that need no version."""
    asc = commands.module(flow, "asc_release")
    checks: list[tuple[str, Any]] = []
    if flow == "scowld":
        checks.append(("no in-app product would have to go to review with the version",
                       lambda: asc.refuse_pending_in_app_products(client, app_id)))
    if flow == "abysm":
        def abysm() -> None:
            items, failures = asc.catalog_review_items(client, app_id, asc.intended_product_ids())
            if failures:
                asc.fail("these required products would not be added to App Review: " + "; ".join(failures))
            print(f"catalog products a tag run would add to the review draft: {len(items)}")
        checks.append(("the catalog products a tag run would add to the review draft", abysm))

    def drafts() -> None:
        draft = asc.find_draft_submission(client, app_id)
        print(f"the version would go out in draft review submission {draft}" if draft
              else "no draft review submission yet: the tag run creates one when it submits")
    checks.append(("the app's draft review submissions", drafts))
    return checks


# What the flow's preflight checks only on the version itself, by the switches that need it.
DEFERRED = (
    ("asc", "the version's state"),
    ("listing", "its localizations and which listing fields would change"),
    ("whats_new", "its What's New localizations"),
    ("screenshots", "its screenshot sets and whether they would change"),
    ("creative", "where the header and search results images would be placed"),
    ("submit", "the version's place in the review draft"),
)
# The flows whose store step attaches the catalog products to the version's draft as it writes, so a
# plan can only check them once the version exists (and even then only the store step does).
PRODUCTS_IN_STORE_STEP = ("verceltics", "pcm-notes", "quit-all")


def deferred(flow: str, resolved: Mapping[str, Any]) -> list[str]:
    items = [label for name, label in DEFERRED if resolved.get(name)]
    if resolved.get("submit") and flow in PRODUCTS_IN_STORE_STEP:
        items.append("the catalog products that go to review with it")
    return items


def ios_plan(app: Any, *, version: str, work_dir: Path, environ: Mapping[str, str] | None = None) -> int:
    env = dict(os.environ if environ is None else environ)
    config = app.config
    flow = config.ios.flow
    release = commands.module(flow, "ios_release")
    tag = config.ios.tag_prefix + version
    try:
        resolved = release.resolve_gates(env, event="push", ref_name=tag, marketing_version=version)
    except release.ReleaseError as exc:
        return finish(flow, "iOS", [f"the switches a tag run would read fail: {exc}"], [], work_dir, env)
    on = [name for name in ("create_version", "upload_build", *SWITCH_ENV) if resolved.get(name)]
    lines = [f"a tag run of {tag} would resolve: {', '.join(on) or 'nothing (every store switch is off)'}"]
    problems: list[str] = []
    if not resolved.get("asc") and not resolved.get("create_version"):
        lines.append("App Store Connect: a tag run would change nothing")
        return finish(flow, "iOS", problems, lines, work_dir, env)
    switches = {variable: "true" if resolved.get(name) else "false" for name, variable in SWITCH_ENV.items()}
    tag_env = {**switches, "GITHUB_REF_NAME": tag, "MARKETING_VERSION": version,
               "GATES_ASC": "true" if resolved.get("asc") else "false"}

    def check(label: str, fn) -> bool:
        with commands.environment(tag_env), read_only(flow):
            ok, problem = run_step(label, fn)
        if not ok:
            problems.append(problem)
        return ok

    offline_ok = all([check(label, fn) for label, fn in offline_checks(config, flow, resolved)])
    asc = commands.module(flow, "asc_release")
    with read_only(flow):
        client = asc.client_from_environment()
        app_id = asc.find_app(client, config.ios.bundle_id)
        versions = getattr(asc, VERSION_LISTS[flow])(client, app_id)
    match = next((row for row in versions if (row.get("attributes") or {}).get("versionString") == version), None)
    if match is None:
        if not resolved.get("create_version"):
            problems.append(f"App Store version {version} does not exist and IOS_CREATE_VERSION is off")
            return finish(flow, "iOS", problems, lines, work_dir, env)
        lines.append(f"App Store version {version} is missing: IOS_CREATE_VERSION would create it first")
        if not resolved.get("asc"):
            return finish(flow, "iOS", problems, lines, work_dir, env)
        # The preflight needs the version; run every check that does not.
        checks = whats_new_checks(flow, client, app_id, "") if resolved.get("whats_new") else []
        if resolved.get("submit"):
            checks += review_checks(flow, client, app_id)
        passed = [check(label, fn) for label, fn in checks]
        if offline_ok and all(passed):
            lines.append("every check that needs no App Store version passed")
        waiting = deferred(flow, resolved)
        note = (f"not checked until the tag run creates App Store version {version}: " + "; ".join(waiting)
                + " (the tag run checks them once it has created the version, before anything is uploaded)")
        lines.append(note)
        print(f"::notice title=store-plan::{note}")
        return finish(flow, "iOS", problems, lines, work_dir, env)
    lines.append(f"App Store version {version} exists")
    if not resolved.get("asc"):
        return finish(flow, "iOS", problems, lines, work_dir, env)
    if check("the preflight a tag run would make (read-only)",
             lambda: commands.preflight(config, flow, ["--version", version])):
        lines.append("the preflight passed: the version can take every enabled operation")
    # A check the flow's store step makes but its preflight does not.
    if flow == "pcm-notes" and resolved.get("whats_new"):
        for label, fn in whats_new_checks(flow, client, app_id, match.get("id") or ""):
            check(label, fn)
    return finish(flow, "iOS", problems, lines, work_dir, env)


def android_plan(app: Any, *, work_dir: Path, environ: Mapping[str, str] | None = None) -> int:
    env = dict(os.environ if environ is None else environ)
    config = app.config
    flow = config.android.flow
    release = commands.module(flow, "android_release")
    version_name, _ = release.gradle_version(release.GRADLE.read_text(encoding="utf-8"))
    tag = config.android.tag_prefix + version_name
    try:
        resolved = release.resolve_gates(env, event="push", ref_name=tag, version_name=version_name)
    except release.ReleaseError as exc:
        return finish(flow, "Android", [f"the switches a tag run would read fail: {exc}"], [], work_dir, env)
    tracks = resolved.get("play_tracks") or ""
    on = [name for name in ("listing", "whats_new", "screenshots", "internal", "closed", "production")
          if resolved.get(name)]
    lines = [f"a tag run of {tag} would resolve: {', '.join(on) or 'nothing (every store switch is off)'}"]
    problems: list[str] = []
    if not resolved.get("github_release"):
        lines.append("Google Play: a tag run would build and release nothing")
        return finish(flow, "Android", problems, lines, work_dir, env)
    if not env.get("PLAY_SERVICE_ACCOUNT_JSON"):
        problems.append("PLAY_SERVICE_ACCOUNT_JSON is not set: a tag run's versionCode check would fail")
        return finish(flow, "Android", problems, lines, work_dir, env)
    with commands.environment({"GITHUB_OUTPUT": str(work_dir / "outputs.txt")}):
        work_dir.mkdir(parents=True, exist_ok=True)
        ok, problem = run_step("the versionCode check a tag run makes (read-only Play edit)",
                               lambda: release.main(["check-version-code"]))
        if not ok:
            problems.append(problem)
        if ok and tracks:
            ok, problem = run_step(f"the Play state of the selected tracks ({tracks})",
                                   lambda: release.main(["play-state", "--tracks", tracks]))
            if not ok:
                problems.append(problem)
    return finish(flow, "Android", problems, lines, work_dir, env)


def main(app: Any, platform: str, args: Any) -> int:
    try:
        if platform == "ios":
            return ios_plan(app, version=args.version, work_dir=args.work_dir)
        return android_plan(app, work_dir=args.work_dir)
    except Exception:  # noqa: BLE001 - report-only: print the traceback and fail the plan job
        traceback.print_exc()
        print("error: the store plan could not be computed (unexpected error above)", file=sys.stderr)
        return 1
