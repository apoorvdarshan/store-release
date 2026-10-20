"""android plan --work-dir D: what a tag run would change on Google Play and GitHub Releases, read-only.

GitHub part: the versionCode preflight and the release the tag would create.
Play part (when a listing, screenshot or track switch is on and the Play key is
set): one throwaway edit opened through the ReadOnlyPublisher reads the live
listing, phone screenshots, uploads and tracks, then is deleted. Report-only:
exit 0 when the plan was computed, 1 only when it could not be.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping

from store_release import __version__
from store_release.android import release as android_release
from store_release.ios.plan import Report, attempt, _module_main  # noqa: PLC2701 - shared report helpers

SWITCH_KEYS = ("listing", "whats_new", "screenshots", "internal", "closed", "production")


class PlanAbort(Exception):
    """The plan cannot be computed (a Play auth or API error)."""


def run_plan(app: Any, *, work_dir: Path, environ: Mapping[str, str] | None = None,
             service: Any = None, gh_runner: Callable | None = None) -> tuple[int, dict]:
    from store_release import github, screenshots  # noqa: PLC0415
    from store_release.text import listing, whats_new  # noqa: PLC0415

    env = dict(os.environ if environ is None else environ)
    config = app.config
    android = config.android
    work_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    result: dict[str, Any] = {"platform": "android", "phases": [], "problems": report.problems, "reads": []}

    def phase(name: str, text: str) -> None:
        result["phases"].append({"name": name, "outcome": text})
        report.line(f"{len(result['phases'])}. {name}: {text}")

    gradle = (app.root / android.gradle_file).read_text(encoding="utf-8")
    try:
        version_name, version_code = android_release.gradle_version(gradle)
    except android_release.ReleaseError as exc:
        report.line(f"store-plan android: {config.app.name}, store-release v{__version__}")
        report.problem(str(exc))
        return finish(report, result, work_dir, env)
    tag = android.tag_prefix + version_name
    sha = env.get("TOOL_SHA", "")
    report.line(f"store-plan android: {config.app.name} {version_name} (versionCode {version_code}, tag {tag}), "
                f"store-release v{__version__}" + (f" ({sha})" if sha else ""))
    result.update(version=version_name, version_code=version_code, tag=tag)
    try:
        planned = android_release.resolve_gates(env, event="push", ref_name=tag, version_name=version_name)
        android_release.check_switch_tables(planned)
    except android_release.ReleaseError as exc:
        number = report.problem(str(exc))
        phase("switches", report.blocked(number))
        return finish(report, result, work_dir, env)
    master = "on" if planned["github_release"] else "off"
    report.line("switches a tag run would use: master=" + master + " " +
                " ".join(f"{key}={'on' if planned[key] else 'off'}" for key in SWITCH_KEYS))
    for note in planned.get("notes", []):
        report.line(f"  note: {note}")
    if not planned["build"]:
        phase("release", "a tag run builds and releases nothing (ANDROID_RELEASE_AUTOMATION is off)")
        return finish(report, result, work_dir, env)

    plan_github(app, planned, tag, version_name, version_code, env, report, phase, result,
                gh_runner or github.run_gh)

    if not (planned["listing"] or planned["screenshots"] or planned["play_upload"]):
        phase("Google Play", "not planned (no listing, screenshot or track switch is on)")
        return finish(report, result, work_dir, env)
    meta = work_dir / "store-meta"
    shots = work_dir / "screenshots"
    local: int | None = None
    if planned["listing"]:
        ok, _, _, problem = attempt(lambda: _module_main(listing, ["--platform", "play", "--out", str(meta)]))
        if not ok:
            local = report.problem(f"listing text: {problem}")
    if planned["whats_new"]:
        ok, _, _, problem = attempt(lambda: _module_main(whats_new, ["--platform", "play", "--tag", tag, "--out",
                                                                     str(meta)]))
        if not ok:
            local = local or report.problem(f"What's New: {problem}")
    if planned["screenshots"]:
        def stage() -> None:
            saved = os.environ.get("STORE_UPLOAD_SCREENSHOTS")
            os.environ["STORE_UPLOAD_SCREENSHOTS"] = "true"
            try:
                screenshots.restage(shots)
                screenshots.stage(dry_run=False, platform="play")
            finally:
                if saved is None:
                    os.environ.pop("STORE_UPLOAD_SCREENSHOTS", None)
                else:
                    os.environ["STORE_UPLOAD_SCREENSHOTS"] = saved

        ok, _, _, problem = attempt(stage)
        if not ok:
            local = local or report.problem(f"screenshots: {problem}")

    if service is None:
        if not env.get("PLAY_SERVICE_ACCOUNT_JSON", "").strip():
            phase("Google Play", "skipped: no Play credentials (PLAY_SERVICE_ACCOUNT_JSON is not set)")
            return finish(report, result, work_dir, env)
        try:
            service = android_release.play_publisher(read_only=True)
        except Exception as exc:  # noqa: BLE001 - an unreadable key cannot be planned with
            raise PlanAbort(f"cannot open the Play API client: {exc}") from None
    elif not isinstance(service, android_release.ReadOnlyPublisher):
        service = android_release.ReadOnlyPublisher(service)
    plan_play(app, planned, version_code, meta, shots, service, report, phase, result, local)
    return finish(report, result, work_dir, env)


def plan_github(app, planned, tag, version_name, version_code, env, report, phase, result, gh_runner) -> None:
    from store_release import github  # noqa: PLC0415

    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo:
        phase("GitHub Release", "not planned (GITHUB_REPOSITORY is not set)")
        return
    listing = gh_runner(["gh", "release", "list", "--repo", repo, "--limit", "1000", "--json", "tagName,name"])
    if listing.returncode != 0:
        phase("GitHub Release", f"not planned (gh release list failed: {(listing.stderr or '').strip()})")
        return
    releases = json.loads(listing.stdout or "[]")
    codes = android_release.released_version_codes(releases, tag)
    try:
        android_release.check_new_on_github(version_code, codes)
        phase("GitHub preflight", f"versionCode {version_code} is higher than every earlier Android GitHub Release "
                                  f"(highest: {max(codes, default=0)})")
    except android_release.ReleaseError as exc:
        number = report.problem(str(exc))
        phase("GitHub preflight", f"refused: {exc}")
        phase("GitHub Release", report.blocked(number))
        return
    base = github.render_template(app.config.android.asset_name, slug=app.config.app.slug, version=version_name,
                                  version_code=str(version_code))
    tags = [row.get("tagName") or "" for row in releases]
    highest = github.is_highest(tag, app.config.android.tag_prefix, tags)
    if planned["production"]:
        kind = "--prerelease=false --latest" if highest else "--prerelease=false --latest=false"
    else:
        kind = "--prerelease --latest=false"
    exists = github.release_exists(tag, repo, gh_runner)
    title = f"Android {version_name} (versionCode {version_code})"
    result["github_release"] = {"title": title, "flags": kind, "exists": exists,
                                "assets": [f"{base}{s}" for s in (".aab", ".aab.sigstore.json", ".apk",
                                                                  ".apk.sigstore.json")]}
    phase("GitHub Release", f"{'update' if exists else 'create'} {tag} titled {title!r} with {kind}; assets "
                            f"{base}.aab, {base}.apk and their .sigstore.json bundles")


def plan_play(app, planned, version_code, meta, shots, service, report, phase, result, local) -> None:
    package = android_release.PACKAGE_NAME
    locale = app.config.android.locale

    def reader(edits, edit_id):
        reads: dict[str, Any] = {}
        if planned["listing"]:
            reads["listing"] = edits.listings().get(packageName=package, editId=edit_id, language=locale).execute()
        if planned["screenshots"]:
            reads["images"] = edits.images().list(packageName=package, editId=edit_id, language=locale,
                                                  imageType="phoneScreenshots").execute()
        if planned["play_upload"]:
            reads["bundles"] = edits.bundles().list(packageName=package, editId=edit_id).execute()
            reads["apks"] = edits.apks().list(packageName=package, editId=edit_id).execute()
            reads["tracks"] = edits.tracks().list(packageName=package, editId=edit_id).execute()
        return reads

    try:
        reads = android_release.read_play_state(service, reader)
    except android_release.ReleaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - Play API errors (auth, package) mean no plan
        raise PlanAbort(f"Google Play: {exc}") from None
    result["reads"] = sorted(reads)
    if planned["listing"]:
        if local:
            phase("Play listing", report.blocked(local))
        else:
            live = reads["listing"] or {}
            fields = []
            for field, filename in (("title", "title.txt"), ("shortDescription", "short_description.txt"),
                                    ("fullDescription", "full_description.txt")):
                path = meta / "play" / locale / filename
                wanted = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
                current = (live.get(field) or "").strip()
                state = "same" if current == wanted else "differs"
                fields.append(f"{field} {state} ({len(current)} → {len(wanted)} chars)")
            phase("Play listing", "would update the listing (one edit, committed): " + ", ".join(fields))
    if planned["screenshots"]:
        live_images = (reads.get("images") or {}).get("images") or []
        staged = sorted((shots / "play" / "phone").glob("*.png"))
        live_digests = {image.get("sha256") or image.get("sha1") for image in live_images}
        matches = sum(1 for path in staged if hashlib.sha256(path.read_bytes()).hexdigest() in live_digests)
        phase("Play screenshots", f"would delete {len(live_images)} live and upload {len(staged)} staged frames "
                                  f"({matches} staged frame(s) already live by sha256)")
    if planned["whats_new"]:
        notes = meta / "play" / f"whatsnew-{locale}"
        if notes.is_file():
            phase("Play What's New", f"{len(notes.read_text(encoding='utf-8'))} characters for {locale}")
        else:
            phase("Play What's New", report.blocked(local) if local else f"no whatsnew-{locale}")
    if planned["play_upload"]:
        tracks = [track for track in planned["play_tracks"].split(",") if track]
        known = android_release.known_version_codes(reads.get("bundles"), reads.get("apks"), reads.get("tracks"))
        by_track = {track.get("track"): track for track in (reads.get("tracks") or {}).get("tracks") or []}
        try:
            decision = android_release.play_upload_decision(version_code, known_codes=known, tracks=by_track,
                                                            selected=tracks, earlier_upload_attempt=None)
            phase("Play upload", f"would {decision} versionCode {version_code} to {', '.join(tracks)} "
                                 f"(highest on Play: {max(known, default=0)})")
        except android_release.ReleaseError as exc:
            report.problem(f"Play upload: {exc}")
            phase("Play upload", f"refused: {exc}")


def finish(report: Report, result: dict, work_dir: Path, env: Mapping[str, str]) -> tuple[int, dict]:
    report.line("writes a tag run would make: listed above per phase; nothing was written")
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
    parser = argparse.ArgumentParser(prog="android plan", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        code, _ = run_plan(app, work_dir=args.work_dir)
        return code
    except PlanAbort as exc:
        print(f"error: the store plan could not be computed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - report-only: print the traceback and fail the plan job
        traceback.print_exc()
        print("error: the store plan could not be computed (unexpected error above)", file=sys.stderr)
        return 1
