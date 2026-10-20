"""The read-only store plan of a ported flow (validation dispatches: ios plan / android plan).

The flow's switches are resolved as a tag run of this version would resolve them (the
same repository variables), then the flow's own read-only checks run against the live
store with a client that refuses every write before it is sent:

  iOS      the App Store version (would IOS_CREATE_VERSION create it?) and the flow's
           read-only preflight with the switches a tag run would have
  Android  the flow's read-only Play checks a tag run makes before building
           (check-version-code; with a track selected, play-state)

Report-only, like the fud-ai flow's plan: it exits 0 when the plan was computed (CLEAN, or
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
    text = out.getvalue().strip()
    print(f"== {label}: {'passed' if ok else 'a tag run would fail here'}")
    if text:
        print(text)
    last = next((line for line in reversed(text.splitlines()) if line.startswith("error:")), "")
    return ok, last.removeprefix("error: ") or f"{label} failed"


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
    asc = commands.module(flow, "asc_release")
    with read_only(flow):
        client = asc.client_from_environment()
        app_id = asc.find_app(client, config.ios.bundle_id)
        versions = getattr(asc, VERSION_LISTS[flow])(client, app_id)
    exists = any((row.get("attributes") or {}).get("versionString") == version for row in versions)
    if not exists:
        if resolved.get("create_version"):
            lines.append(f"App Store version {version} is missing: IOS_CREATE_VERSION would create it, and the "
                         "preflight runs against it after that")
            return finish(flow, "iOS", problems, lines, work_dir, env)
        problems.append(f"App Store version {version} does not exist and IOS_CREATE_VERSION is off")
        return finish(flow, "iOS", problems, lines, work_dir, env)
    lines.append(f"App Store version {version} exists")
    if not resolved.get("asc"):
        return finish(flow, "iOS", problems, lines, work_dir, env)
    switches = {variable: "true" if resolved.get(name) else "false" for name, variable in SWITCH_ENV.items()}
    with commands.environment({**switches, "GITHUB_REF_NAME": tag}), read_only(flow):
        ok, problem = run_step("the preflight a tag run would make (read-only)",
                               lambda: commands.preflight(config, flow, ["--version", version]))
    if not ok:
        problems.append(problem)
    else:
        lines.append("the preflight passed: the version can take every enabled operation")
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
