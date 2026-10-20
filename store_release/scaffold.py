"""`run.py init`: scaffold store-release for a new app.

    python3 -I -u store-release/run.py --app-root . init --platform ios|android|all --name "My App" [...]

Writes the app's store/store-release.toml (or --config), its caller workflows
(ios-release.yml, android-release.yml, store-checks.yml) rendered from that config and
pinned to a store-release release, and .github/dependabot.yml when the app has none.
Values come from flags; in a terminal, a missing required value is asked for instead.
The config is validated before anything is written, and existing files are never
overwritten without --force. Only the standard flow is scaffolded: ported flows exist
for the apps they were ported from (docs/flows.md).

Nothing is uploaded and no switch is turned on: every switch defaults to off, so a tag
pushed before any switch is set changes no store (an iOS tag still gets its GitHub
Release). The printed next steps name the secrets and variables to set.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from store_release import __version__, callers
from store_release import config as config_mod

DEFAULT_DEVELOPER_DIR = "/Applications/Xcode_27.app/Contents/Developer"
DEFAULT_IOS_TESTS = ".github/workflows/ios-tests.yml"
DEFAULT_ANDROID_TESTS = ".github/workflows/android-tests.yml"
DEPENDABOT = ".github/dependabot.yml"
REMOTE = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")
LOCAL_WORKFLOW_FILE = re.compile(r"\.github/workflows/([A-Za-z0-9._-]+)\.ya?ml")


# The config key each flag sets, to name the flag when the generated config is invalid.
FLAGS = {
    "app.name": "--name", "app.slug": "--slug", "ios.tag_prefix": "--ios-tag-prefix", "ios.bundle_id": "--bundle-id",
    "ios.asc_app_id": "--asc-app-id", "ios.team_id": "--team-id", "ios.project": "--project", "ios.scheme": "--scheme",
    "ios.app_target": "--app-target", "ios.info_plist": "--info-plist", "ios.gate_jobs.id": "--ios-tests",
    "ios.gate_jobs.uses": "--ios-tests", "ios.build.developer_dir": "--developer-dir", "ios.build.runner": "--runner",
    "android.tag_prefix": "--android-tag-prefix", "android.package": "--package",
    "android.upload_cert_sha256": "--upload-cert-sha256", "android.gradle_file": "--android-dir",
    "android.gate_jobs.id": "--android-tests", "android.gate_jobs.uses": "--android-tests",
    "android.build.dir": "--android-dir", "android.build.checkout": "--android-dir",
}


class InitError(config_mod.ConfigError):
    """A missing or invalid answer, or a file init would overwrite."""


@dataclass
class Answers:
    platform: str
    name: str
    slug: str
    repository: str = callers.DEFAULT_REPOSITORY
    version: str = __version__
    app_repository: str = ""
    config_path: str = config_mod.DEFAULT_CONFIG
    ios: dict[str, str] = field(default_factory=dict)
    android: dict[str, str] = field(default_factory=dict)

    @property
    def has_ios(self) -> bool:
        return self.platform in ("ios", "all")

    @property
    def has_android(self) -> bool:
        return self.platform in ("android", "all")


# --- answers -----------------------------------------------------------------------


def slug_for(name: str) -> str:
    """A default app.slug from the app's name: lowercase letters, digits and single hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")
    return slug or "app"


def normalize_cert(value: str) -> str:
    """An upload certificate SHA-256 as Play Console or keytool prints it (colons, any case) as 64 hex."""
    return re.sub(r"[\s:]", "", value).lower()


def app_repository_from_git(app_root: Path) -> str:
    """owner/name from the app checkout's origin remote, or "" when it has none on github.com."""
    try:
        result = subprocess.run(["git", "-C", str(app_root), "remote", "get-url", "origin"], capture_output=True,
                                text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = REMOTE.fullmatch(result.stdout.strip()) if result.returncode == 0 else None
    return match.group(1) if match else ""


def detect_info_plist(app_root: Path, project: str, target: str) -> str:
    """The app target's INFOPLIST_FILE, read from the project when it is in the checkout; else Xcode's
    default layout (<project dir>/<target>/Info.plist) when that file exists; else "" (generated)."""
    from store_release.ios import release as ios_release  # noqa: PLC0415

    if not project.endswith(".xcodeproj") or not target:
        return ""
    pbxproj = app_root / project / "project.pbxproj"
    if pbxproj.is_file():
        try:
            configs = ios_release.app_configurations(pbxproj, target)
        except ios_release.ReleaseError:
            configs = {}
        files = set()
        for settings in configs.values():
            relative = re.sub(r"^\$[({]SRCROOT[)}]/", "", settings.get("INFOPLIST_FILE", ""))
            if relative:
                files.add((Path(project).parent / relative).as_posix())
        if len(files) == 1:
            return files.pop()
        if configs and not files:
            return ""
    candidate = (Path(project).parent / target / "Info.plist").as_posix()
    return candidate if (app_root / candidate).is_file() else ""


def gate_id(workflow: str, key: str) -> str:
    match = LOCAL_WORKFLOW_FILE.fullmatch(workflow)
    if not match:
        raise InitError(f"{key}: {workflow!r} must be a workflow file in .github/workflows/ (for example "
                        f"{DEFAULT_IOS_TESTS})")
    job = re.sub(r"[^a-z0-9_-]+", "-", match.group(1).lower()).strip("-_") or "tests"
    return job if config_mod.JOB_ID.fullmatch(job) else f"tests-{job}"


def collect(ns: argparse.Namespace, app_root: Path, *, ask: Callable[[str], str] | None,
            environ: dict[str, str]) -> Answers:
    """The answers from the flags, asking for each missing required value when ask is given."""
    missing: list[str] = []

    def value(flag: str, label: str, current: str | None, default: str | None = None) -> str:
        if current:
            return current
        if ask is not None:
            suffix = f" [{default}]" if default else ""
            answer = ask(f"{label}{suffix}: ").strip()
            if answer or default:
                return answer or default or ""
        elif default is not None:
            return default
        missing.append(flag)
        return ""

    platform = value("--platform", "Platforms (ios, android or all)", ns.platform)
    if platform and platform not in ("ios", "android", "all"):
        raise InitError(f"--platform: {platform!r} must be ios, android or all")
    name = value("--name", "App name", ns.name)
    slug = value("--slug", "Slug (temporary file names, asset names)", ns.slug, slug_for(name) if name else None)
    answers = Answers(platform=platform, name=name, slug=slug, repository=ns.repository, version=ns.version,
                      config_path=ns.config_path)
    both = platform == "all"
    if answers.has_ios:
        project = value("--project", "Xcode project (path to the .xcodeproj)", ns.project)
        # Without a project there is no default to offer; --project is already reported missing.
        scheme_default = Path(project).stem if project.endswith(".xcodeproj") else ("" if not project else None)
        scheme = value("--scheme", "Scheme to archive", ns.scheme, scheme_default)
        app_target = value("--app-target", "App target", ns.app_target, scheme if scheme or not project else None)
        answers.ios = {
            "bundle_id": value("--bundle-id", "iOS bundle id", ns.bundle_id),
            "asc_app_id": value("--asc-app-id", "App Store Connect Apple ID (App Information, digits)",
                                ns.asc_app_id),
            "team_id": value("--team-id", "Apple Developer team id (10 characters)", ns.team_id),
            "project": project,
            "scheme": scheme,
            "app_target": app_target,
            "info_plist": ns.info_plist if ns.info_plist is not None else detect_info_plist(app_root, project,
                                                                                            app_target),
            "tag_prefix": value("--ios-tag-prefix", "iOS tag prefix", ns.ios_tag_prefix, "ios-v" if both else "v"),
            "tests": value("--ios-tests", "iOS test workflow", ns.ios_tests, DEFAULT_IOS_TESTS),
            "developer_dir": value("--developer-dir", "Xcode developer dir", ns.developer_dir, DEFAULT_DEVELOPER_DIR),
            "runner": ns.runner or "",
        }
    if answers.has_android:
        answers.android = {
            "package": value("--package", "Android applicationId", ns.package),
            "upload_cert_sha256": normalize_cert(value(
                "--upload-cert-sha256", "Play upload key certificate SHA-256", ns.upload_cert_sha256)),
            "tag_prefix": value("--android-tag-prefix", "Android tag prefix", ns.android_tag_prefix,
                                "android-v" if both else "v"),
            "dir": value("--android-dir", "Gradle project folder", ns.android_dir, "android"),
            "tests": value("--android-tests", "Android test workflow", ns.android_tests, DEFAULT_ANDROID_TESTS),
        }
        app_repository = ns.app_repository or environ.get("GITHUB_REPOSITORY", "") or app_repository_from_git(app_root)
        answers.app_repository = value("--app-repository", "App repository (owner/name)", app_repository)
    else:
        answers.app_repository = ns.app_repository or ""
    if missing:
        raise InitError("init needs " + ", ".join(dict.fromkeys(missing)) + " (or run it in a terminal to be asked)")
    return answers


# --- the config --------------------------------------------------------------------


def toml_str(value: str) -> str:
    out = []
    for ch in value:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _checkout_for_project(project: str) -> list[str]:
    parent = Path(project).parent.as_posix()
    if parent != ".":
        return [f"/{parent.split('/')[0]}/"]
    return [f"/{Path(project).stem}/", f"/{project}/"]


def render_config(answers: Answers) -> str:
    """The app's store-release.toml for these answers (validated by config.parse before it is written)."""
    docs = f"https://github.com/{answers.repository}/blob/v{answers.version}/docs"
    lines = [
        f"# store-release configuration for {answers.name}, written by `run.py init`.",
        f"# Every key: {docs}/config.md",
        "# Switches are repository variables and credentials repository secrets; nothing here is secret.",
        "schema = 1",
        "",
        "[app]",
        f"name = {toml_str(answers.name)}",
        f"slug = {toml_str(answers.slug)}",
    ]
    if answers.has_ios:
        ios = answers.ios
        lines += [
            "",
            "[ios]",
            "# Tags <tag_prefix><MARKETING_VERSION> release; Run workflow only validates.",
            f"tag_prefix = {toml_str(ios['tag_prefix'])}",
            f"bundle_id = {toml_str(ios['bundle_id'])}",
            "# App Store Connect -> the app -> App Information -> Apple ID.",
            f"asc_app_id = {toml_str(ios['asc_app_id'])}",
            f"team_id = {toml_str(ios['team_id'])}",
            f"project = {toml_str(ios['project'])}",
            f"scheme = {toml_str(ios['scheme'])}",
            f"app_target = {toml_str(ios['app_target'])}",
        ]
        if ios["info_plist"]:
            lines.append(f"info_plist = {toml_str(ios['info_plist'])}")
        else:
            lines.append("# No info_plist: Xcode generates the app's Info.plist (GENERATE_INFOPLIST_FILE = YES).")
        lines += [
            "# The app's own tests (a workflow with `on: workflow_call`): a tag releases only after they pass.",
            "gate_jobs = [",
            f"  {{ id = {toml_str(gate_id(ios['tests'], '--ios-tests'))}, uses = {toml_str('./' + ios['tests'])} }},",
            "]",
            "",
            "[ios.build]",
            f"developer_dir = {toml_str(ios['developer_dir'])}",
        ]
        if ios["runner"]:
            lines.append(f"runner = {toml_str(ios['runner'])}")
        lines += [
            "# What the build job checks out (a sparse checkout): the project and its sources.",
            "checkout = [" + ", ".join(toml_str(p) for p in _checkout_for_project(ios["project"])) + "]",
            "",
            "# Each IOS_UPLOAD_* switch needs its table; uncomment one when you turn its switch on.",
            "# [ios.listing]              # IOS_UPLOAD_LISTING: the fenced fields of APPSTORE.md",
            "# [ios.whats_new]            # IOS_UPLOAD_WHATS_NEW: the What's New section of APPSTORE.md",
            "# [[ios.screenshots.sets]]   # IOS_UPLOAD_SCREENSHOTS, one table per display type",
            '# display_type = "APP_IPHONE_67"',
            '# dir = "store/screenshots/iphone"',
            "# required = true",
            "# [ios.creative]             # IOS_UPLOAD_CREATIVE_ASSETS: store/creative/ios/{header,search-results}/",
            "# [ios.products]             # IOS_SUBMIT_REVIEW",
        ]
    if answers.has_android:
        android = answers.android
        build_dir = android["dir"]
        lines += [
            "",
            "[android]",
            "# Tags <tag_prefix><versionName> release; Run workflow only validates.",
            f"tag_prefix = {toml_str(android['tag_prefix'])}",
            f"package = {toml_str(android['package'])}",
        ]
        if build_dir != "android":
            lines.append(f"gradle_file = {toml_str(build_dir + '/app/build.gradle.kts')}")
        lines += [
            "# Play Console -> Test and release -> App integrity -> Upload key certificate (SHA-256).",
            "# Nothing signed by another key is ever published.",
            f"upload_cert_sha256 = {toml_str(android['upload_cert_sha256'])}",
            "# The app's own tests (a workflow with `on: workflow_call`): a tag releases only after they pass.",
            "gate_jobs = [",
            f"  {{ id = {toml_str(gate_id(android['tests'], '--android-tests'))}, "
            f"uses = {toml_str('./' + android['tests'])} }},",
            "]",
            "",
            "[android.build]",
        ]
        if build_dir != "android":
            lines.append(f"dir = {toml_str(build_dir)}")
        lines += [
            "# What the release job checks out (a sparse checkout).",
            f"checkout = [{toml_str('/' + build_dir.split('/')[0] + '/')}]",
            "# The upload key reaches Gradle as <dir>/keystore.properties (storeFile, storePassword,",
            "# keyAlias, keyPassword); the app must gitignore that file.",
            'signing = "keystore-properties"',
            "",
            "# Each ANDROID_UPLOAD_* switch needs its table; uncomment one when you turn its switch on.",
            "# [android.listing]          # ANDROID_UPLOAD_LISTING: the numbered fields of PLAYSTORE.md",
            "# [android.whats_new]        # ANDROID_UPLOAD_WHATS_NEW: the What's New section of PLAYSTORE.md",
            "# [android.screenshots]      # ANDROID_UPLOAD_SCREENSHOTS",
            '# dir = "store/screenshots/play"',
        ]
    return "\n".join(lines) + "\n"


def render_dependabot(repository: str) -> str:
    pattern = f"{repository}*"
    return "\n".join([
        "# Dependabot keeps the store-release pin and the other GitHub Actions up to date.",
        "version: 2",
        "updates:",
        "  - package-ecosystem: github-actions",
        "    directory: /",
        "    schedule:",
        "      interval: weekly",
        "    groups:",
        "      # store-release's callers move together, in a pull request of their own (the first",
        "      # group a dependency matches wins, so this one comes first).",
        "      store-release:",
        f'        patterns: ["{pattern}"]',
        "      github-actions:",
        '        patterns: ["*"]',
        "    # These pins run with the app's signing and store credentials: a new store-release",
        "    # release waits seven days before it is proposed. Security updates never wait.",
        "    cooldown:",
        "      default-days: 7",
        f'      include: ["{pattern}"]',
        "",
    ])


# --- files -------------------------------------------------------------------------


def planned_files(answers: Answers, *, sha: str) -> dict[str, str]:
    """{path relative to the app root: text} for everything init writes (dependabot.yml included)."""
    text = render_config(answers)
    try:
        cfg = config_mod.parse(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - render_config writes valid TOML
        raise InitError(f"the generated config is not valid TOML: {exc}") from None
    except config_mod.ConfigError as exc:
        key = str(exc).split(":", 1)[0]
        flag = FLAGS.get(re.sub(r"\[\d+\]", "", key))
        raise InitError(f"{exc}{f' ({flag})' if flag else ''}") from None
    files = {answers.config_path: text}
    platforms = [p for p, has in (("ios", answers.has_ios), ("android", answers.has_android)) if has] + ["checks"]
    for platform in platforms:
        files[f".github/workflows/{callers.CALLER_FILES[platform]}"] = callers.render(
            cfg, platform, sha=sha, version=answers.version, repository=answers.repository,
            app_repository=answers.app_repository, config_path=answers.config_path)
    files[DEPENDABOT] = render_dependabot(answers.repository)
    return files


def resolve_sha(answers: Answers, sha: str | None, *, allow_untagged: bool, opener=None) -> str:
    """The commit to pin: --sha (which must be the v<version> tag's commit), else the tag's commit."""
    if sha:
        if not callers.SHA40.fullmatch(sha):
            raise InitError(f"--sha {sha!r} is not a 40-hex commit SHA")
        if not allow_untagged:
            callers.check_release_tag(sha, answers.repository, version=answers.version, opener=opener)
        return sha
    commit = callers.release_tag_commit(answers.repository, answers.version, opener=opener)
    if commit is None:
        raise InitError(f"{answers.repository} has no release tag v{answers.version}; run init from a release "
                        "checkout, or pass --version and --sha of a release")
    return commit


def next_steps(answers: Answers, written: list[str], app_root: Path, *, dependabot_written: bool) -> list[str]:
    steps = []
    project = answers.ios.get("project")
    if project and not (app_root / project / "project.pbxproj").is_file():
        steps.append(f"{project} is not in {app_root}: check --project, --scheme, --app-target and "
                     "--info-plist in the config.")
    tests = [(answers.ios.get("tests"), "iOS"), (answers.android.get("tests"), "Android")]
    for workflow, label in tests:
        if workflow and not (app_root / workflow).is_file():
            steps.append(f"Add {workflow}: your {label} tests, with `on: workflow_call` (the release runs it).")
        elif workflow:
            steps.append(f"Make sure {workflow} has `on: workflow_call` (the release runs it as a gate job).")
    if not dependabot_written:
        steps.append(f"{DEPENDABOT} exists, so it was left alone: add the store-release group of the README's "
                     "quickstart to it, so Dependabot proposes each new store-release release.")
    secrets: list[str] = []
    if answers.has_ios:
        secrets += [*callers.ASC_SECRETS, *callers.SIGNING_SECRETS]
    if answers.has_android:
        secrets += [*callers.KEYSTORE_SECRETS, callers.PLAY_SECRET]
    steps.append("Set the repository secrets (docs/secrets.md): " + ", ".join(secrets) + ".")
    masters = [name for name, has in (("IOS_RELEASE_AUTOMATION", answers.has_ios),
                                      ("ANDROID_RELEASE_AUTOMATION", answers.has_android)) if has]
    steps.append("Every switch is off until you set it (docs/switches.md); start with "
                 + " and ".join(masters) + ", for example `gh variable set " + masters[0] + " --body true`.")
    steps.append("Check the files: python3 -I -u <store-release>/run.py --app-root . config check --files")
    runs = [label for label, has in (("iOS Release", answers.has_ios), ("Android Release", answers.has_android)) if has]
    steps.append("Commit, then Run workflow on " + " and ".join(runs) + ": a validation run builds and signs, "
                 "uploads nothing, and prints a read-only store plan.")
    return [f"wrote {path}" for path in written] + ["", "next:"] + [f"  {i}. {step}" for i, step in enumerate(steps, 1)]


def write(app_root: Path, files: dict[str, str], *, force: bool) -> tuple[list[str], bool]:
    """Writes the files (dependabot.yml only when absent); returns (written, dependabot written)."""
    for relative in files:
        config_mod.check_path(relative, "init")
        config_mod.resolve_inside(app_root, relative, "init")
    exists = [rel for rel in files if rel != DEPENDABOT and (app_root / rel).exists()]
    if exists and not force:
        raise InitError("init would overwrite " + ", ".join(exists) + " (pass --force to replace them)")
    written = []
    dependabot = not (app_root / DEPENDABOT).exists()
    for relative, text in files.items():
        if relative == DEPENDABOT and not dependabot:
            continue
        path = app_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(relative)
    return written, dependabot


# The answers examples/templates/<name>/ were written with (scripts/render_examples.py; a test
# keeps them equal to what init writes).
PLACEHOLDER_SHA = "0" * 40
TEMPLATE_IOS = ["--name", "Example App", "--bundle-id", "com.example.app", "--asc-app-id", "1234567890",
                "--team-id", "ABCDE12345", "--project", "ios/ExampleApp.xcodeproj", "--info-plist",
                "ios/ExampleApp/Info.plist"]
TEMPLATE_ANDROID = ["--name", "Example App", "--package", "com.example.app", "--upload-cert-sha256", "0" * 64,
                    "--app-repository", "owner/example-app"]
TEMPLATES = {
    "ios-only": ["--platform", "ios", *TEMPLATE_IOS],
    "android-only": ["--platform", "android", *TEMPLATE_ANDROID],
    "ios-and-android": ["--platform", "all", *TEMPLATE_IOS, *TEMPLATE_ANDROID[2:]],
}


def template_files(name: str) -> dict[str, str]:
    """{path in examples/templates/<name>/: text}: what init writes for the template's answers."""
    ns = parser().parse_args([*TEMPLATES[name], "--sha", PLACEHOLDER_SHA, "--allow-untagged"])
    ns.config_path = config_mod.DEFAULT_CONFIG
    answers = collect(ns, Path("/nonexistent-template-root"), ask=None, environ={})
    files = planned_files(answers, sha=PLACEHOLDER_SHA)
    out = {}
    for relative, text in files.items():
        if relative == answers.config_path:
            out["store-release.toml"] = text
        elif relative == DEPENDABOT:
            out["dependabot.yml"] = text
        else:
            out[f"callers/{Path(relative).name}"] = text
    return out


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="init", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--platform", choices=("ios", "android", "all"))
    p.add_argument("--name", help="app.name (shown in messages)")
    p.add_argument("--slug", help="app.slug (default: from --name)")
    ios = p.add_argument_group("iOS")
    ios.add_argument("--bundle-id")
    ios.add_argument("--asc-app-id", help="App Store Connect -> App Information -> Apple ID")
    ios.add_argument("--team-id")
    ios.add_argument("--project", help="path to the .xcodeproj, relative to the app root")
    ios.add_argument("--scheme", help="default: the project's name")
    ios.add_argument("--app-target", help="default: the scheme")
    ios.add_argument("--info-plist", help="the app target's Info.plist (default: <project dir>/<scheme>/Info.plist "
                                          "when it exists; empty when Xcode generates it)")
    ios.add_argument("--ios-tag-prefix", help="default: v (ios-v with --platform all)")
    ios.add_argument("--ios-tests", help=f"the iOS test workflow (default: {DEFAULT_IOS_TESTS})")
    ios.add_argument("--developer-dir", help=f"default: {DEFAULT_DEVELOPER_DIR}")
    ios.add_argument("--runner", help="ios.build.runner: the macOS runner label (default: ios-build.yml's)")
    android = p.add_argument_group("Android")
    android.add_argument("--package", help="the applicationId")
    android.add_argument("--upload-cert-sha256", help="Play upload key certificate SHA-256 (colons allowed)")
    android.add_argument("--android-tag-prefix", help="default: v (android-v with --platform all)")
    android.add_argument("--android-dir", help="the Gradle project folder (default: android)")
    android.add_argument("--android-tests", help=f"the Android test workflow (default: {DEFAULT_ANDROID_TESTS})")
    android.add_argument("--app-repository", help="owner/name of the app repository (default: the origin remote)")
    pin = p.add_argument_group("pin")
    pin.add_argument("--version", default=__version__, help="the store-release release to pin (default: this one)")
    pin.add_argument("--sha", help="its commit (default: looked up from the v<version> tag)")
    pin.add_argument("--repository", default=callers.DEFAULT_REPOSITORY, help="store-release's repository")
    pin.add_argument("--allow-untagged", action="store_true", help="pin --sha without the tag check (tests only)")
    p.add_argument("--force", action="store_true", help="replace existing config and caller files")
    p.add_argument("--no-input", action="store_true", help="never ask: a missing required value fails")
    return p


def main(app_root: Path | None, argv: list[str], *, config_path: str = config_mod.DEFAULT_CONFIG,
         ask: Callable[[str], str] | None = None, environ: dict[str, str] | None = None, opener=None) -> int:
    ns = parser().parse_args(argv)
    if app_root is None:
        raise InitError("init writes into the app: pass --app-root DIR (usually .)")
    if not app_root.is_dir():
        raise InitError(f"--app-root {app_root} is not a folder")
    ns.config_path = config_path
    if ask is None and not ns.no_input and sys.stdin.isatty():
        ask = input
    answers = collect(ns, app_root, ask=ask, environ=dict(os.environ if environ is None else environ))
    sha = resolve_sha(answers, ns.sha, allow_untagged=ns.allow_untagged, opener=opener)
    files = planned_files(answers, sha=sha)
    written, dependabot = write(app_root, files, force=ns.force)
    print("\n".join(next_steps(answers, written, app_root, dependabot_written=dependabot)))
    return 0
