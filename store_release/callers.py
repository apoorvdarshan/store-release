"""An app's caller workflows: rendered from its config, checked at run time and in pull requests.

  callers render     the exact text of ios-release.yml, android-release.yml and store-checks.yml
                     for this config, store-release commit and version. It is the only source
                     of caller text: examples/ and every app's callers come from it. It refuses
                     a commit the version's release tag does not point at (--allow-untagged
                     is for store-release's own CI).
  caller check --pr  (PyYAML) the app's callers parse to exactly the rendered ones (comments
                     aside), every store-release ref is this commit with its version comment,
                     and the v<version> release tag points at this commit.
  caller check --runtime
                     (stdlib, in prepare/gates) every store-release ref in the running caller is
                     this job's commit, and its concurrency is exactly the release queue. A
                     release tag that does not match (or cannot be read) is only a warning: a
                     release never depends on mutable remote state.
  pins check         every store-release ref in any app workflow is this commit and version,
                     and the v<version> release tag points at this commit.

The tag checks run inside the pinned code, so they catch honest mistakes (a pin to an
untagged commit, a hand-edited version comment), not a hostile pin: a commit from a fork
also resolves through `uses:` by SHA and could skip them (docs/verification.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from store_release import __version__
from store_release.config import DEFAULT_CONFIG, Config, ConfigError, render_template

DEFAULT_REPOSITORY = "apoorvdarshan/store-release"
SHA40 = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CALLER_FILES = {"ios": "ios-release.yml", "android": "android-release.yml", "checks": "store-checks.yml"}
# A plain job with either name would satisfy an app's required status check by accident.
FORBIDDEN_JOB_NAMES = ("web", "android")
ASC_SECRETS = ("APP_STORE_CONNECT_API_KEY_ID", "APP_STORE_CONNECT_ISSUER_ID", "APP_STORE_CONNECT_API_KEY_P8")
SIGNING_SECRETS = ("IOS_DEV_SIGNING_P12_BASE64", "IOS_DEV_SIGNING_P12_PASSWORD")
KEYSTORE_SECRETS = ("ANDROID_KEYSTORE_BASE64", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD")
PLAY_SECRET = "PLAY_SERVICE_ACCOUNT_JSON"
GATE_LABELS = {"ios-tests": "the iOS tests", "quality": "quality checks"}
TAG_LOOKUP_TIMEOUT = 30
# An annotated tag may point at another tag; follow at most this many before giving up.
MAX_TAG_DEPTH = 5


class CallerError(ConfigError):
    """A caller workflow differs from what this store-release version renders, or is pinned wrongly."""


# --- release tags ----------------------------------------------------------------


def release_tag_commit(repository: str, version: str, *, environ: Mapping[str, str] | None = None,
                       opener: Callable[..., Any] | None = None) -> str | None:
    """The commit store-release's release tag v<version> points at (annotated tags peeled), or None
    when the tag does not exist.

    Reads GET /repos/<repository>/git/ref/tags/v<version> (and /git/tags/<sha> for an annotated
    tag) from GITHUB_API_URL, with GH_TOKEN in the Authorization header when it is set. A 404
    means there is no such tag; any other failure raises CallerError, so callers fail closed.
    """
    env = os.environ if environ is None else environ
    opener = opener or urllib.request.urlopen
    if not REPOSITORY.fullmatch(repository or ""):
        raise CallerError(f"the store-release repository {repository!r} is not owner/name")
    if not VERSION.fullmatch(version or ""):
        raise CallerError(f"version {version!r} must look like 0.1.0")
    api = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if env.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {env['GH_TOKEN']}"
    label = f"{repository} tag v{version}"

    def get(path: str) -> dict | None:
        request = urllib.request.Request(f"{api}/repos/{repository}/{path}", headers=headers)
        try:
            with opener(request, timeout=TAG_LOOKUP_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise CallerError(f"could not read the {label} (HTTP {exc.code}); re-run") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CallerError(f"could not read the {label} ({exc}); re-run") from None
        if not isinstance(data, dict):
            raise CallerError(f"could not read the {label}: unexpected response; re-run")
        return data

    ref = get(f"git/ref/tags/v{version}")
    if ref is None:
        return None
    target = ref.get("object") if isinstance(ref.get("object"), dict) else {}
    for _ in range(MAX_TAG_DEPTH):
        kind, sha = target.get("type"), target.get("sha")
        if not isinstance(sha, str) or not SHA40.fullmatch(sha):
            break
        if kind == "commit":
            return sha
        if kind != "tag":
            break
        tag = get(f"git/tags/{sha}")
        target = tag.get("object") if tag is not None and isinstance(tag.get("object"), dict) else {}
    raise CallerError(f"the {label} does not resolve to a commit")


def check_release_tag(sha: str, repository: str, *, environ: Mapping[str, str] | None = None,
                      opener: Callable[..., Any] | None = None, version: str = __version__) -> str:
    """Fail unless store-release's v<version> tag points at sha, the commit an app pinned."""
    commit = release_tag_commit(repository, version, environ=environ, opener=opener)
    if commit is None:
        raise CallerError(f"{repository} has no release tag v{version}: pin the commit of a store-release "
                          f"release, never an untagged commit ({sha})")
    if commit != sha:
        raise CallerError(f"{repository} v{version} is {commit}, not the pinned {sha}: pin the commit the "
                          f"v{version} tag points at")
    return f"the v{version} tag points at {sha}"


def _uses(repository: str, workflow: str, sha: str, version: str) -> str:
    return f"uses: {repository}/.github/workflows/{workflow}@{sha} # v{version}"


def _secrets(names: tuple[str, ...]) -> list[str]:
    return ["    secrets:"] + [f"      {name}: ${{{{ secrets.{name} }}}}" for name in names]


def _with(values: list[tuple[str, str]], config_path: str) -> list[str]:
    if config_path != DEFAULT_CONFIG:
        values = [("config", f'"{config_path}"'), *values]
    if not values:
        return []
    return ["    with:"] + [f"      {key}: {value}" for key, value in values]


def _human(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _gate_labels(ids: list[str]) -> str:
    return _human([GATE_LABELS.get(job_id, f"the {job_id} job") for job_id in ids])


def _pronoun(ids: list[str]) -> str:
    return {1: "it", 2: "both"}.get(len(ids), "them")


def _header_rows(rows: list[tuple[str, list[str]]]) -> list[str]:
    width = max(20, max(len(left) for left, _ in rows) + 3)
    lines = []
    for left, right in rows:
        lines.append(f"#   {left:<{width}}{right[0]}".rstrip())
        lines.extend(f"#   {'':<{width}}{line}" for line in right[1:])
    return lines


def _gate_inputs(job) -> list[str]:
    """A gate job's `with:` inputs (ios.gate_jobs[].with / android.gate_jobs[].with)."""
    if not job.inputs:
        return []

    def scalar(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        return f'"{value}"'

    return ["    with:"] + [f"      {name}: {scalar(value)}" for name, value in job.inputs]


def _source_archive_job(repository: str, sha: str, version: str, config_path: str, release_if: list[str],
                        gate_results: str, needs) -> list[str]:
    """ios.github_release.source_archive: the job that archives, signs and attests the tagged source."""
    return [
        "  # The tagged source as an archive, signed with Sigstore and attested (it alone needs id-token).",
        "  source-archive:",
        f"    needs: {needs('prepare', 'build', 'wait-for-build', 'app-store-connect')}",
        "    if: >-",
        *release_if,
        f"    {_uses(repository, 'ios-source-archive.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        "      id-token: write",
        "      attestations: write",
        *_with([("gates", f'"{gate_results}"'),
                ("marketing_version", "${{ needs.prepare.outputs.marketing_version }}"),
                ("uploaded_build", "${{ needs.build.result == 'success' && needs.prepare.outputs.build_number || '' }}"),
                ("tool_sha", "${{ needs.prepare.outputs.tool_sha }}")], config_path),
        "",
    ]


def _regex_repository(repository: str) -> str:
    return repository.replace(".", r"\.")


def render_ios(config: Config, *, sha: str, version: str, repository: str = DEFAULT_REPOSITORY,
               config_path: str = DEFAULT_CONFIG) -> str:
    ios = config.ios
    if ios is None:
        raise CallerError("no [ios] in store-release.toml; there is no iOS caller to render")
    gates = [job.id for job in ios.gate_jobs]
    prefix = ios.tag_prefix
    gate_results = " ".join(f"{gate}=${{{{ needs.{gate}.result }}}}" for gate in gates)
    all_success = " && ".join(f"needs.{gate}.result == 'success'" for gate in gates)
    all_skipped = " && ".join(f"needs.{gate}.result == 'skipped'" for gate in gates)
    if ios.gate_policy == "parallel":
        needs = lambda *extra: "[" + ", ".join([*gates, *extra]) + "]"  # noqa: E731
    else:
        # The gate jobs need prepare, so prepare comes first.
        needs = lambda *extra: "[" + ", ".join([*extra[:1], *gates, *extra[1:]]) + "]"  # noqa: E731
    archive_job = ("source-archive",) if ios.source_archive else ()
    if ios.gate_policy != "when-uploading":
        release_if = [
            "      !cancelled() && github.event_name == 'push' &&",
            *[f"      needs.{gate}.result == 'success' &&" for gate in gates[:-1]],
            f"      needs.{gates[-1]}.result == 'success' && needs.prepare.result == 'success' &&",
            "      (needs.build.result == 'success' || needs.build.result == 'skipped') &&",
            "      (needs.wait-for-build.result == 'success' || needs.wait-for-build.result == 'skipped') &&",
            "      (needs.app-store-connect.result == 'success' || needs.app-store-connect.result == 'skipped')",
        ]
    else:
        release_if = [
            "      !cancelled() && github.event_name == 'push' && needs.prepare.result == 'success' &&",
            "      (needs.prepare.outputs.upload_build != 'true' ||",
            f"       ({all_success} &&",
            "        needs.build.result == 'success' && needs.wait-for-build.result == 'success')) &&",
            "      (needs.prepare.outputs.asc != 'true' || needs.app-store-connect.result == 'success')",
        ]
    lines = [
        f"# iOS Release: a thin caller of {repository}, pinned by commit SHA.",
        "# Rendered by `run.py callers render --platform ios`; Store checks fails when it drifts.",
        "#",
        *_header_rows([
            (f"push tag {prefix}X.Y", ["release run (the tag must equal MARKETING_VERSION)"]),
            ("Run workflow", ["validation run: archive, sign, export and validate the IPA with",
                              "App Store Connect; never uploads. store-plan prints, read-only,",
                              "what a tag run would change in App Store Connect."]),
        ]),
        "#",
        "# Switches (repository variables), secrets, re-runs and verification:",
        f"# https://github.com/{repository}/tree/v{version}/docs",
        "name: iOS Release",
        "",
        "on:",
        "  push:",
        "    tags:",
        f'      - "{prefix}*"',
        "  workflow_dispatch:",
        "    inputs:",
        "      marketing_version:",
        "        description: >-",
        "          Validation only: version to validate as (e.g. 7.2). Empty = the project",
        "          version. Apple rejects versions already approved, so use the next one.",
        "        required: false",
        '        default: ""',
        "",
        "permissions:",
        "  contents: read",
        "",
        "# Build numbers come from App Store Connect, so tag release runs never overlap.",
        "# Manual validation runs get their own group and can never cancel a release run.",
        "concurrency:",
        "  group: ios-release-${{ github.event_name == 'push' && 'release' || github.run_id }}",
        "  cancel-in-progress: false",
        "  # Keep every waiting tag run queued; the default (single) would cancel an older pending run.",
        "  queue: max",
        "",
        "jobs:",
    ]
    policy = ios.gate_policy
    for job in ios.gate_jobs:
        if policy == "parallel":
            lines += [f"  {job.id}:", "    if: github.event_name == 'push'"]
        elif policy == "after-prepare":
            lines += [f"  {job.id}:", "    needs: prepare", "    if: github.event_name == 'push'"]
        else:
            lines += [f"  {job.id}:", "    needs: prepare",
                      "    if: needs.prepare.outputs.mode == 'release' && needs.prepare.outputs.upload_build == 'true'"]
        lines += [f"    uses: {job.uses}", "    permissions:", "      contents: read", *_gate_inputs(job), ""]
    lines += [
        "  prepare:",
        f"    {_uses(repository, 'ios-prepare.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([("marketing_version", "${{ inputs.marketing_version }}")], config_path),
        *_secrets(ASC_SECRETS),
        "",
        "  build:",
        f"    needs: {needs('prepare')}",
        f"    # Tag runs need {_gate_labels(gates)} to pass; validation runs skip {_pronoun(gates)}.",
        "    if: >-",
        "      !cancelled() && needs.prepare.result == 'success' &&",
        "      needs.prepare.outputs.build == 'true' &&",
        f"      (({all_success}) ||",
        *(["       (github.event_name == 'workflow_dispatch' &&"] if policy != "when-uploading"
          else ["       (needs.prepare.outputs.mode == 'validate' &&"]),
        f"        {all_skipped}))",
        f"    {_uses(repository, 'ios-build.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([*([("runner", f'"{ios.build.runner}"')] if ios.build.runner else []),
                ("gates", f'"{gate_results}"'),
                ("mode", "${{ needs.prepare.outputs.mode }}"),
                ("marketing_version", "${{ needs.prepare.outputs.marketing_version }}"),
                ("build_number", "${{ needs.prepare.outputs.build_number }}"),
                ("tool_sha", "${{ needs.prepare.outputs.tool_sha }}")], config_path),
        *_secrets(ASC_SECRETS + SIGNING_SECRETS),
        "",
        "  # Its own job, so \"Re-run failed jobs\" after a later store-step failure keeps this",
        "  # result (and build_id) instead of waiting again against the original upload deadline.",
        "  wait-for-build:",
        f"    needs: {needs('prepare', 'build')}",
        "    if: >-",
        "      !cancelled() && needs.prepare.result == 'success' &&",
        "      needs.prepare.outputs.mode == 'release' &&",
        "      needs.prepare.outputs.upload_build == 'true' &&",
        f"      {all_success} &&",
        "      needs.build.result == 'success'",
        f"    {_uses(repository, 'ios-wait-for-build.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([("marketing_version", "${{ needs.prepare.outputs.marketing_version }}"),
                ("build_number", "${{ needs.prepare.outputs.build_number }}"),
                ("upload_started_at", "${{ needs.build.outputs.upload_started_at }}"),
                ("wait_minutes", "${{ needs.prepare.outputs.wait_minutes }}"),
                ("poll_seconds", "${{ needs.prepare.outputs.poll_seconds }}"),
                ("tool_sha", "${{ needs.prepare.outputs.tool_sha }}")], config_path),
        *_secrets(ASC_SECRETS),
        "",
        "  app-store-connect:",
        f"    needs: {needs('prepare', 'build', 'wait-for-build')}",
        "    # With IOS_UPLOAD_BUILD on, only after this run's build is processed; with it off,",
        "    # listing/What's New/screenshots/creative assets run without a build.",
        "    if: >-",
        "      !cancelled() && needs.prepare.result == 'success' &&",
        "      needs.prepare.outputs.mode == 'release' &&",
        "      needs.prepare.outputs.asc == 'true' &&",
        *([f"      {all_success} &&",
           "      ((needs.build.result == 'success' && needs.wait-for-build.result == 'success') ||",
           "       (needs.prepare.outputs.upload_build != 'true' &&",
           "        needs.build.result == 'skipped' && needs.wait-for-build.result == 'skipped'))"]
          if policy != "when-uploading" else
          ["      ((needs.prepare.outputs.upload_build == 'true' &&",
           f"        {all_success} &&",
           "        needs.build.result == 'success' && needs.wait-for-build.result == 'success') ||",
           "       (needs.prepare.outputs.upload_build != 'true' && needs.wait-for-build.result == 'skipped'))"]),
        f"    {_uses(repository, 'ios-app-store-connect.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([("gates", f'"{gate_results}"'),
                ("marketing_version", "${{ needs.prepare.outputs.marketing_version }}"),
                ("build_number", "${{ needs.prepare.outputs.build_number }}"),
                ("build_id", "${{ needs.wait-for-build.outputs.build_id }}"),
                ("upload_build", "${{ needs.prepare.outputs.upload_build }}"),
                ("listing", "${{ needs.prepare.outputs.listing }}"),
                ("whats_new", "${{ needs.prepare.outputs.whats_new }}"),
                ("screenshots", "${{ needs.prepare.outputs.screenshots }}"),
                ("creative", "${{ needs.prepare.outputs.creative }}"),
                ("submit", "${{ needs.prepare.outputs.submit }}"),
                ("public_release", "${{ needs.prepare.outputs.public_release }}"),
                ("tool_sha", "${{ needs.prepare.outputs.tool_sha }}")], config_path),
        *_secrets(ASC_SECRETS),
        "",
        *(_source_archive_job(repository, sha, version, config_path, release_if, gate_results, needs)
          if ios.source_archive else []),
        "  github-release:",
        f"    needs: {needs('prepare', 'build', 'wait-for-build', 'app-store-connect', *archive_job)}",
        "    if: >-",
        *(release_if[:-1] + [release_if[-1] + " &&", "      needs.source-archive.result == 'success'"]
          if ios.source_archive else release_if),
        f"    {_uses(repository, 'ios-github-release.yml', sha, version)}",
        "    permissions:",
        "      contents: write",
        *_with([("gates", f'"{gate_results}"'),
                ("marketing_version", "${{ needs.prepare.outputs.marketing_version }}"),
                ("uploaded_build", "${{ needs.build.result == 'success' && needs.prepare.outputs.build_number || '' }}"),
                ("tool_sha", "${{ needs.prepare.outputs.tool_sha }}")], config_path),
        "",
        "  # Validation runs only: a read-only, report-only plan of what a tag run would change.",
        "  store-plan:",
        "    needs: prepare",
        "    if: github.event_name == 'workflow_dispatch' && needs.prepare.result == 'success'",
        f"    {_uses(repository, 'ios-store-plan.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([("marketing_version", "${{ needs.prepare.outputs.marketing_version }}"),
                ("tool_sha", "${{ needs.prepare.outputs.tool_sha }}")], config_path),
        *_secrets(ASC_SECRETS),
    ]
    return "\n".join(lines) + "\n"


def render_android(config: Config, *, sha: str, version: str, app_repository: str,
                   repository: str = DEFAULT_REPOSITORY, config_path: str = DEFAULT_CONFIG) -> str:
    android = config.android
    if android is None:
        raise CallerError("no [android] in store-release.toml; there is no Android caller to render")
    if not REPOSITORY.fullmatch(app_repository or ""):
        raise CallerError(f"the app repository {app_repository!r} is not owner/name (--app-repository)")
    gates = [job.id for job in android.gate_jobs]
    prefix = android.tag_prefix
    asset = render_template(android.asset_name, slug=config.app.slug, version="X.Y", version_code="N")
    gate_results = " ".join(f"{gate}=${{{{ needs.{gate}.result }}}}" for gate in gates)
    all_success = " && ".join(f"needs.{gate}.result == 'success'" for gate in gates)
    all_skipped = " && ".join(f"needs.{gate}.result == 'skipped'" for gate in gates)
    success = f"({all_success} ||" if len(gates) == 1 else f"(({all_success}) ||"
    lines = [
        f"# Android Release: a thin caller of {repository}, pinned by commit SHA.",
        "# Rendered by `run.py callers render --platform android`; Store checks fails when it drifts.",
        "#",
        *_header_rows([
            (f"push tag {prefix}X.Y", ["release run (the tag must equal versionName)"]),
            ("Run workflow", ["validation run: build the signed AAB and APK and verify them;",
                              "never uploads, signs with Sigstore or releases. store-plan",
                              "prints, read-only, what a tag run would change."]),
        ]),
        "#",
        "# Verifying a GitHub Release download built by store-release (docs/verification.md):",
        f"#   cosign verify-blob {asset}.apk --bundle {asset}.apk.sigstore.json \\",
        f"#     --certificate-identity-regexp '^https://github\\.com/{_regex_repository(repository)}"
        "/\\.github/workflows/android-publish\\.yml@[0-9a-f]{40}$' \\",
        "#     --certificate-oidc-issuer https://token.actions.githubusercontent.com \\",
        f"#     --certificate-github-workflow-repository {app_repository}",
        f"#   gh attestation verify {asset}.apk --repo {app_repository} \\",
        f"#     --signer-workflow {repository}/.github/workflows/android-publish.yml",
        "name: Android Release",
        "",
        "on:",
        "  push:",
        "    tags:",
        f'      - "{prefix}*"',
        "  workflow_dispatch:",
        "",
        "permissions:",
        "  contents: read",
        "",
        "# Tag release runs never overlap. Manual validation runs get their own group and",
        "# can never cancel a release run.",
        "concurrency:",
        "  group: android-release-${{ github.event_name == 'push' && 'release' || github.run_id }}",
        "  cancel-in-progress: false",
        "  # Keep every waiting tag run queued; the default (single) would cancel an older pending run.",
        "  queue: max",
        "",
        "jobs:",
        "  gates:",
        f"    {_uses(repository, 'android-gates.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        "      actions: read",
        *_with([], config_path),
        "    secrets:",
        f"      {PLAY_SECRET}: ${{{{ secrets.{PLAY_SECRET} }}}}",
        "",
    ]
    for job in android.gate_jobs:
        lines += [f"  {job.id}:", "    needs: gates",
                  "    if: needs.gates.outputs.mode == 'release' && needs.gates.outputs.build == 'true'",
                  f"    uses: {job.uses}", "    permissions:", "      contents: read", *_gate_inputs(job), ""]
    keystore_only = android.build.signing == "gradle-properties"
    keystore_secrets = KEYSTORE_SECRETS[:2] if keystore_only else KEYSTORE_SECRETS
    lines += [
        "  release:",
        "    needs: [" + ", ".join(["gates", *gates]) + "]",
        f"    # Validation runs skip {_human(gates)}; release runs need {'it' if len(gates) == 1 else 'them'} to pass. A tag with",
        "    # ANDROID_RELEASE_AUTOMATION off resolves build=false, so this is skipped: a green no-op.",
        "    if: >-",
        "      !cancelled() && needs.gates.result == 'success' && needs.gates.outputs.build == 'true' &&",
        f"      {success}",
        f"       (needs.gates.outputs.mode == 'validate' && {all_skipped}))",
        f"    {_uses(repository, 'android-publish.yml', sha, version)}",
        "    permissions:",
        "      contents: write",
        "      id-token: write",
        "      attestations: write",
        "      actions: read",
        *_with([*([("keystore_only", "true")] if keystore_only else []),
                ("gates", f'"{gate_results}"'),
                ("mode", "${{ needs.gates.outputs.mode }}"),
                ("build", "${{ needs.gates.outputs.build }}"),
                ("github_release", "${{ needs.gates.outputs.github_release }}"),
                ("listing", "${{ needs.gates.outputs.listing }}"),
                ("whats_new", "${{ needs.gates.outputs.whats_new }}"),
                ("screenshots", "${{ needs.gates.outputs.screenshots }}"),
                ("production", "${{ needs.gates.outputs.production }}"),
                ("play_upload", "${{ needs.gates.outputs.play_upload }}"),
                ("play_tracks", "${{ needs.gates.outputs.play_tracks }}"),
                ("tool_sha", "${{ needs.gates.outputs.tool_sha }}")], config_path),
        *_secrets(keystore_secrets + (PLAY_SECRET,)),
        "",
        "  # Validation runs only: a read-only, report-only plan of what a tag run would change.",
        "  store-plan:",
        "    needs: gates",
        "    if: github.event_name == 'workflow_dispatch' && needs.gates.result == 'success'",
        f"    {_uses(repository, 'android-store-plan.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([("tool_sha", "${{ needs.gates.outputs.tool_sha }}")], config_path),
        "    secrets:",
        f"      {PLAY_SECRET}: ${{{{ secrets.{PLAY_SECRET} }}}}",
    ]
    return "\n".join(lines) + "\n"


def render_checks(config: Config, *, sha: str, version: str, repository: str = DEFAULT_REPOSITORY,
                  config_path: str = DEFAULT_CONFIG) -> str:
    def paths_block() -> list[str]:
        if not config.checks_paths:
            return []
        return ["    paths:"] + [f'      - "{path}"' for path in config.checks_paths]

    lines = [
        "# Store checks: validates the store data, the store-release config and these callers",
        "# against the pinned store-release version. Never uploads anything. Safe to run anytime.",
        "# Rendered by `run.py callers render --platform checks`.",
        "name: Store checks",
        "",
        "on:",
        "  push:",
        "    branches: [main]",
        *paths_block(),
        "  pull_request:",
        *paths_block(),
        "  workflow_dispatch:",
        "",
        "permissions:",
        "  contents: read",
        "",
        "# A newer push to a pull request cancels its older run; main and manual runs are never cancelled.",
        "concurrency:",
        "  group: store-checks-${{ github.event_name == 'pull_request' && "
        "format('pr-{0}', github.event.pull_request.number) || github.run_id }}",
        "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
        "",
        "jobs:",
        "  checks:",
        f"    {_uses(repository, 'checks.yml', sha, version)}",
        "    permissions:",
        "      contents: read",
        *_with([], config_path),
        "",
        f"  # One stable check name across store-release versions (not a required check in v{version}).",
        "  store-release:",
        "    needs: checks",
        "    if: ${{ always() }}",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 5",
        "    # It reads only the checks job's result: no token permission at all.",
        "    permissions: {}",
        "    steps:",
        "      - name: Require the store checks to pass",
        "        env:",
        "          RESULT: ${{ needs.checks.result }}",
        '        run: test "$RESULT" = success',
    ]
    return "\n".join(lines) + "\n"


def render(config: Config, platform: str, *, sha: str, version: str, repository: str = DEFAULT_REPOSITORY,
           app_repository: str = "", config_path: str = DEFAULT_CONFIG) -> str:
    if not SHA40.fullmatch(sha or ""):
        raise CallerError(f"--sha {sha!r} is not a 40-hex commit SHA")
    if not VERSION.fullmatch(version or ""):
        raise CallerError(f"--version {version!r} must look like 0.1.0")
    if not REPOSITORY.fullmatch(repository or ""):
        raise CallerError(f"--repository {repository!r} is not owner/name")
    if platform == "ios":
        return render_ios(config, sha=sha, version=version, repository=repository, config_path=config_path)
    if platform == "android":
        return render_android(config, sha=sha, version=version, repository=repository, app_repository=app_repository,
                              config_path=config_path)
    if platform == "checks":
        return render_checks(config, sha=sha, version=version, repository=repository, config_path=config_path)
    raise CallerError(f"unknown caller {platform!r}")


def render_main(config: Config, argv: list[str], *, config_path: str = DEFAULT_CONFIG) -> int:
    parser = argparse.ArgumentParser(prog="callers render")
    parser.add_argument("--platform", required=True, choices=sorted(CALLER_FILES))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--app-repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--config-in-app", default=config_path,
                        help="the config path the app's callers name (default: --config)")
    parser.add_argument("--allow-untagged", action="store_true",
                        help="render a commit the --version tag does not point at (store-release's own CI only)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    text = render(config, args.platform, sha=args.sha, version=args.version, repository=args.repository,
                  app_repository=args.app_repository, config_path=args.config_in_app)
    if not args.allow_untagged:
        print(check_release_tag(args.sha, args.repository, version=args.version), file=sys.stderr)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"rendered {args.out}")
    else:
        sys.stdout.write(text)
    return 0


# --- checks ----------------------------------------------------------------------


def _tool_identity(environ: dict[str, str]) -> tuple[str, str]:
    sha = environ.get("TOOL_SHA", "")
    repository = environ.get("TOOL_REPOSITORY", "")
    if not SHA40.fullmatch(sha):
        raise CallerError(f"TOOL_SHA {sha!r} is not this job's 40-hex job.workflow_sha")
    if not REPOSITORY.fullmatch(repository):
        raise CallerError(f"TOOL_REPOSITORY {repository!r} is not owner/name (job.workflow_repository)")
    return sha, repository


def store_release_refs(text: str, repository: str) -> list[tuple[int, str, str, str]]:
    """(line number, workflow file, ref, rest of the line) for every uses: of a store-release workflow."""
    pattern = re.compile(rf"^\s*(?:-\s+)?uses:\s*{re.escape(repository)}/\.github/workflows/([A-Za-z0-9._-]+)"
                         r"@([^\s#]*)(.*)$")
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = pattern.match(line)
        if match:
            found.append((number, match.group(1), match.group(2), match.group(3)))
        elif f"{repository}/.github/workflows/" in line and not line.lstrip().startswith("#"):
            found.append((number, "?", "", line))
    return found


def concurrency_lines(text: str) -> list[str]:
    """The top-level concurrency block's non-comment lines, whitespace-normalized."""
    lines = text.splitlines()
    found: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            if line.rstrip() == "concurrency:":
                inside = True
            continue
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            found.append(" ".join(stripped.split()))
    return found


def runtime_check(app_root: Path, platform: str, *, environ: dict[str, str] | None = None,
                  opener: Callable[..., Any] | None = None) -> None:
    from store_release import paths  # noqa: PLC0415

    env = dict(os.environ if environ is None else environ)
    sha, repository = _tool_identity(env)
    relative = paths.caller_path(env.get("GITHUB_WORKFLOW_REF", ""), env.get("GITHUB_REPOSITORY", ""))
    path = app_root / relative
    if not path.is_file():
        raise CallerError(f"the caller workflow {relative} is not in the checkout")
    text = path.read_text(encoding="utf-8")
    refs = store_release_refs(text, repository)
    if not refs:
        raise CallerError(f"{relative} calls no {repository} workflow")
    wrong = [f"line {number}: {workflow}@{ref or '?'}" for number, workflow, ref, _ in refs if ref != sha]
    if wrong:
        raise CallerError(f"every {repository} ref in {relative} must be pinned to {sha} (this job's commit); "
                          f"found {'; '.join(wrong)}")
    expected = [f"group: {platform}-release-${{{{ github.event_name == 'push' && 'release' || github.run_id }}}}",
                "cancel-in-progress: false", "queue: max"]
    found = concurrency_lines(text)
    if found != expected:
        raise CallerError(f"{relative} must keep exactly this concurrency (release runs queue, never cancel): "
                          f"{expected}; found {found}")
    print(f"{relative}: {len(refs)} store-release ref(s) pinned to {sha}; release concurrency queues")
    # A warning only, like a yanked version: Store checks fail on it, a release never depends on it.
    try:
        print(check_release_tag(sha, repository, environ=env, opener=opener))
    except CallerError as exc:
        print(f"::warning title=store-release pin::{exc} (Store checks fail on this; this run goes on)")


def _normalize(value: Any) -> Any:
    """PyYAML reads the key `on` as the boolean True; put it back."""
    if isinstance(value, dict):
        return {("on" if key is True else key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def differences(found: Any, expected: Any, path: str = "") -> list[str]:
    if isinstance(found, dict) and isinstance(expected, dict):
        result = []
        for key in sorted(set(found) | set(expected), key=str):
            here = f"{path}.{key}" if path else str(key)
            if key not in found:
                result.append(f"{here}: missing")
            elif key not in expected:
                result.append(f"{here}: not expected")
            else:
                result += differences(found[key], expected[key], here)
        return result
    if isinstance(found, list) and isinstance(expected, list):
        if len(found) != len(expected):
            return [f"{path}: {len(found)} items, expected {len(expected)}"]
        result = []
        for index, (a, b) in enumerate(zip(found, expected)):
            result += differences(a, b, f"{path}[{index}]")
        return result
    return [] if found == expected else [f"{path}: {found!r}, expected {expected!r}"]


def _job_names(document: Any) -> list[str]:
    jobs = document.get("jobs") if isinstance(document, dict) else None
    return list(jobs) if isinstance(jobs, dict) else []


def pr_check(config: Config, app_root: Path, *, environ: dict[str, str] | None = None,
             config_path: str = DEFAULT_CONFIG, opener: Callable[..., Any] | None = None) -> None:
    import yaml  # noqa: PLC0415 - pull request checks only (requirements-checks.txt)

    env = dict(os.environ if environ is None else environ)
    sha, repository = _tool_identity(env)
    platforms = [name for name, table in (("ios", config.ios), ("android", config.android)) if table is not None]
    problems: list[str] = []
    for platform in [*platforms, "checks"]:
        name = CALLER_FILES[platform]
        path = app_root / ".github" / "workflows" / name
        if not path.is_file():
            problems.append(f"{name}: missing (render it with `run.py callers render --platform {platform}`)")
            continue
        text = path.read_text(encoding="utf-8")
        expected_text = render(config, platform, sha=sha, version=__version__, repository=repository,
                               app_repository=env.get("GITHUB_REPOSITORY", ""), config_path=config_path)
        try:
            found = _normalize(yaml.safe_load(text))
        except yaml.YAMLError as exc:
            problems.append(f"{name}: not valid YAML ({exc})")
            continue
        expected = _normalize(yaml.safe_load(expected_text))
        for job in _job_names(found):
            if job in FORBIDDEN_JOB_NAMES:
                problems.append(f"{name}: a job named {job!r} would satisfy the required '{job}' check by accident")
        problems += [f"{name}: {line}" for line in differences(found, expected)]
        for number, workflow, ref, rest in store_release_refs(text, repository):
            if ref != sha or rest.strip() != f"# v{__version__}":
                problems.append(f"{name} line {number}: {workflow}@{ref or '?'}{rest} must be @{sha} # v{__version__}")
    if problems:
        raise CallerError("the caller workflows differ from what store-release v"
                          f"{__version__} renders:\n  " + "\n  ".join(problems))
    tag = check_release_tag(sha, repository, environ=env, opener=opener)
    print(f"callers match store-release v{__version__} ({', '.join(CALLER_FILES[p] for p in [*platforms, 'checks'])}); "
          f"{tag}")


def pins_check(app_root: Path, *, environ: dict[str, str] | None = None,
               opener: Callable[..., Any] | None = None) -> None:
    env = dict(os.environ if environ is None else environ)
    sha, repository = _tool_identity(env)
    workflows = app_root / ".github" / "workflows"
    problems: list[str] = []
    count = 0
    for path in sorted(list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml"))):
        for number, workflow, ref, rest in store_release_refs(path.read_text(encoding="utf-8"), repository):
            count += 1
            if ref != sha or rest.strip() != f"# v{__version__}":
                problems.append(f"{path.name} line {number}: {workflow}@{ref or '?'}{rest}")
    if problems:
        raise CallerError(f"every {repository} pin must be @{sha} # v{__version__} (half-upgraded or "
                          "hand-edited):\n  " + "\n  ".join(problems))
    tag = check_release_tag(sha, repository, environ=env, opener=opener)
    print(f"{count} store-release pin(s) are {sha} # v{__version__}; {tag}")
