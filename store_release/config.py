"""The app's store/store-release.toml: schema 1, strict validation and path safety.

Unknown tables or keys, a value of the wrong type (a bool must be a TOML bool,
never the string "true"), an enum value this version does not know, and an
unsafe path are all errors. Every error names the dotted key. Nothing in the
file is secret; switches stay in repository variables and secrets in
repository secrets.
"""
from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1
DEFAULT_CONFIG = "store/store-release.toml"

# The fixed tables of store_release.screenshots / ios.asc (code, not config).
IOS_DISPLAY_TYPES = ("APP_IPHONE_67", "APP_IPAD_PRO_3GEN_129", "APP_IPHONE_DUO", "WATCH")
IOS_STAGE_JOBS = ("prepare", "build", "wait-for-build", "app-store-connect", "github-release", "store-plan")
ANDROID_STAGE_JOBS = ("gates", "release", "store-plan")
# A plain job with either name would satisfy an app's required status check by accident.
RESERVED_JOB_NAMES = ("web", "android", "store-release", "checks")

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TAG_PREFIX = re.compile(r"([a-z]+-)*v")
BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
ASC_APP_ID = re.compile(r"[0-9]{6,12}")
TEAM_ID = re.compile(r"[A-Z0-9]{10}")
SCHEME = re.compile(r"[^\"\n\r\x00]{1,128}")
LOCALE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]+)*")
GROUP = re.compile(r"[A-Za-z_]+")
ID_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
LOCAL_WORKFLOW = re.compile(r"\./\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
CERT_SHA256 = re.compile(r"[0-9a-f]{64}")
GRADLE_TASK = re.compile(r":?[A-Za-z0-9_.:-]+")
SPARSE_PATTERN = re.compile(r"!?/[^\n]*[^\s]")
PATH_FILTER = re.compile(r"!?[A-Za-z0-9._/*?\[\]-]+")
ASSET_NAME = re.compile(r"[A-Za-z0-9._-]+")

IOS_TITLE_PLACEHOLDERS = ("version", "build")
ASSET_NAME_PLACEHOLDERS = ("slug", "version", "version_code")
EXPORT_COMPLIANCE_ANSWERS = ("NO", "YES")
# store_release.ios.asc.CREATIVE_SLOTS: each slot folder and the file types it takes.
CREATIVE_SLOT_SUFFIXES = {"header": (".png",), "search-results": (".png", ".jpg", ".jpeg")}


class ConfigError(Exception):
    """A config value is missing, unknown, of the wrong type or unsafe."""


@dataclass(frozen=True)
class GateJob:
    id: str
    uses: str


@dataclass(frozen=True)
class App:
    name: str
    slug: str


@dataclass(frozen=True)
class Catalog:
    path: str
    groups: tuple[str, ...]
    id_prefix: str
    play_rules: bool = False
    revenuecat: str | None = None
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScreenshotSet:
    display_type: str
    dir: str
    required: bool


@dataclass(frozen=True)
class IosBuild:
    developer_dir: str
    checkout: tuple[str, ...]
    package_cache_files: tuple[str, ...] = ("**/Package.resolved",)
    pre_archive: tuple[str, ...] = ()


@dataclass(frozen=True)
class Listing:
    source: str
    markdown: str


@dataclass(frozen=True)
class WhatsNew:
    source: str
    markdown: str
    heading: str


@dataclass(frozen=True)
class Ios:
    tag_prefix: str
    bundle_id: str
    asc_app_id: str
    team_id: str
    project: str
    scheme: str
    app_target: str
    info_plist: str
    locale: str
    gate_jobs: tuple[GateJob, ...]
    build: IosBuild
    listing: Listing | None = None
    whats_new: WhatsNew | None = None
    screenshot_sets: tuple[ScreenshotSet, ...] | None = None
    creative_dir: str | None = None
    iap_route: str | None = None
    title: str = "iOS {version} (build {build})"
    # Pins on the app's own files (config check --files and --pinned): the export compliance
    # answer the project must declare, and the creative images as ("<slot>/<file>", bytes) pairs.
    export_compliance: str | None = None
    creative_sizes: tuple[tuple[str, int], ...] | None = None

    @property
    def pbxproj(self) -> str:
        return f"{self.project}/project.pbxproj"


@dataclass(frozen=True)
class AndroidBuild:
    dir: str
    tasks: tuple[str, ...]
    aab: str
    apk: str
    checkout: tuple[str, ...]
    signing: str
    keystore_properties: str


@dataclass(frozen=True)
class Android:
    tag_prefix: str
    package: str
    gradle_file: str
    upload_cert_sha256: str
    locale: str
    gate_jobs: tuple[GateJob, ...]
    build: AndroidBuild
    listing: Listing | None = None
    whats_new: WhatsNew | None = None
    screenshots_dir: str | None = None
    pad_to_9_16: bool = True
    asset_name: str = "{slug}-v{version}"


@dataclass(frozen=True)
class Config:
    schema: int
    app: App
    release_notes: str
    catalog: Catalog | None
    checks_paths: tuple[str, ...]
    ios: Ios | None
    android: Android | None


# --- value checks -------------------------------------------------------------

_MISSING = object()


def _type_name(value: Any) -> str:
    return {bool: "a bool", int: "an integer", str: "a string", list: "an array", dict: "a table",
            float: "a float"}.get(type(value), type(value).__name__)


def check_path(value: Any, key: str, *, suffix: str | None = None) -> str:
    """A repository-relative path: '/'-separated, no absolute, empty, '.' or '..' segment."""
    if not isinstance(value, str):
        raise ConfigError(f"{key}: expected a path string, got {_type_name(value)}")
    if not value:
        raise ConfigError(f"{key}: the path is empty")
    if any(ch in value for ch in "\\\x00\n\r"):
        raise ConfigError(f"{key}: {value!r} contains a backslash, NUL or newline")
    if value.startswith("/"):
        raise ConfigError(f"{key}: {value!r} must be relative to the app repository root")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ConfigError(f"{key}: {value!r} has an empty, '.' or '..' segment")
    if suffix and not value.endswith(suffix):
        raise ConfigError(f"{key}: {value!r} must end with {suffix}")
    return value


def check_glob(value: Any, key: str) -> str:
    """A repository-relative glob (the path rules, with * ? and ** allowed in segments)."""
    return check_path(value, key)


def check_abs_path(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key}: expected an absolute path string, got {_type_name(value)}")
    if not value.startswith("/") or any(ch in value for ch in "\\\x00\n\r"):
        raise ConfigError(f"{key}: {value!r} must be an absolute path without backslash, NUL or newline")
    if any(part in (".", "..") for part in value.split("/")):
        raise ConfigError(f"{key}: {value!r} has a '.' or '..' segment")
    return value


def check_sparse_pattern(value: Any, key: str) -> str:
    """A non-cone sparse-checkout (gitignore-style) pattern rooted at the repository."""
    if not isinstance(value, str):
        raise ConfigError(f"{key}: expected a pattern string, got {_type_name(value)}")
    if not SPARSE_PATTERN.fullmatch(value) or "\r" in value or "\x00" in value:
        raise ConfigError(f"{key}: {value!r} must look like /dir/ or /file or !/pattern (rooted, no trailing space)")
    if ".." in value.lstrip("!").split("/"):
        raise ConfigError(f"{key}: {value!r} has a '..' segment")
    return value


def check_template(value: Any, key: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key}: expected a non-empty template string")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigError(f"{key}: the template contains a newline or NUL")
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise ConfigError(f"{key}: {value!r} is not a valid template ({exc})") from None
    names = ", ".join("{" + name + "}" for name in allowed)
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed or spec or conversion:
            raise ConfigError(f"{key}: unknown placeholder {{{field}}} in {value!r}; use {names}")
    return value


def render_template(template: str, **values: str) -> str:
    return template.format(**values)


def resolve_inside(app_root: Path, relative: str, key: str = "path") -> Path:
    """The path under app_root, following symlinks; it must stay inside app_root."""
    root = app_root.resolve()
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise ConfigError(f"{key}: {relative!r} resolves outside the app repository ({resolved})")
    return resolved


# --- table reading ------------------------------------------------------------


class _Table:
    """One TOML table; keys are taken one by one, so leftovers are unknown keys."""

    def __init__(self, data: Any, dotted: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{dotted}: expected a table, got {_type_name(data)}")
        self.data = data
        self.dotted = dotted
        self.used: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self.dotted}.{name}" if self.dotted else name

    def has(self, name: str) -> bool:
        return name in self.data

    def raw(self, name: str, default: Any = _MISSING) -> Any:
        self.used.add(name)
        if name not in self.data:
            if default is _MISSING:
                raise ConfigError(f"{self.key(name)}: required key is missing")
            return default
        return self.data[name]

    def str(self, name: str, default: Any = _MISSING, *, pattern: re.Pattern | None = None) -> Any:
        value = self.raw(name, default)
        if value is default and default is not _MISSING:
            return value
        if not isinstance(value, str):
            raise ConfigError(f"{self.key(name)}: expected a string, got {_type_name(value)}")
        if pattern is not None and not pattern.fullmatch(value):
            raise ConfigError(f"{self.key(name)}: {value!r} does not match {pattern.pattern}")
        return value

    def bool(self, name: str, default: Any = _MISSING) -> Any:
        value = self.raw(name, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{self.key(name)}: expected a bool (true/false, unquoted), got {_type_name(value)}")
        return value

    def int(self, name: str, default: Any = _MISSING) -> Any:
        value = self.raw(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self.key(name)}: expected an integer, got {_type_name(value)}")
        return value

    def enum(self, name: str, allowed: tuple[str, ...], default: Any = _MISSING) -> str:
        value = self.str(name, default)
        if value not in allowed:
            raise ConfigError(
                f"{self.key(name)}: {value!r} is not supported by this store-release version "
                f"(accepted: {', '.join(allowed)})"
            )
        return value

    def list(self, name: str, default: Any = _MISSING, *, nonempty: bool = False) -> list:
        value = self.raw(name, default)
        if not isinstance(value, list):
            raise ConfigError(f"{self.key(name)}: expected an array, got {_type_name(value)}")
        if nonempty and not value:
            raise ConfigError(f"{self.key(name)}: must not be empty")
        return value

    def path(self, name: str, default: Any = _MISSING, *, suffix: str | None = None) -> Any:
        value = self.raw(name, default)
        if value is None:
            return None
        return check_path(value, self.key(name), suffix=suffix)

    def table(self, name: str) -> _Table | None:
        if name not in self.data:
            self.used.add(name)
            return None
        return _Table(self.raw(name), self.key(name))

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise ConfigError(f"{self.key(unknown[0])}: unknown key")


def _gate_jobs(table: _Table, reserved: tuple[str, ...]) -> tuple[GateJob, ...]:
    rows = table.list("gate_jobs", nonempty=True)
    jobs: list[GateJob] = []
    for index, row in enumerate(rows):
        entry = _Table(row, f"{table.key('gate_jobs')}[{index}]")
        job_id = entry.str("id", pattern=JOB_ID)
        uses = entry.str("uses", pattern=LOCAL_WORKFLOW)
        entry.done()
        if job_id in reserved:
            raise ConfigError(f"{entry.key('id')}: {job_id!r} is a reserved job name")
        if job_id in {job.id for job in jobs}:
            raise ConfigError(f"{entry.key('id')}: {job_id!r} is listed twice")
        jobs.append(GateJob(job_id, uses))
    return tuple(jobs)


def _patterns(table: _Table, name: str) -> tuple[str, ...]:
    values = table.list(name, nonempty=True)
    return tuple(check_sparse_pattern(value, f"{table.key(name)}[{i}]") for i, value in enumerate(values))


def _hooks(table: _Table, name: str) -> tuple[str, ...]:
    values = table.list(name, [])
    hooks = []
    for i, value in enumerate(values):
        key = f"{table.key(name)}[{i}]"
        check_path(value, key)
        if not value.endswith((".sh", ".py")):
            raise ConfigError(f"{key}: {value!r} must be a .sh or .py script")
        hooks.append(value)
    return tuple(hooks)


def _listing(table: _Table | None, default_markdown: str) -> Listing | None:
    if table is None:
        return None
    listing = Listing(table.enum("source", ("markdown",), "markdown"),
                      table.path("markdown", default_markdown))
    table.done()
    return listing


def _whats_new(table: _Table | None, default_markdown: str) -> WhatsNew | None:
    if table is None:
        return None
    source = table.enum("source", ("markdown",), "markdown")
    markdown = table.path("markdown", default_markdown)
    heading = table.str("heading", "What's New")
    if not heading or not heading.isprintable():
        raise ConfigError(f"{table.key('heading')}: must be non-empty printable text")
    table.done()
    return WhatsNew(source, markdown, heading)


def _screenshot_sets(table: _Table | None) -> tuple[ScreenshotSet, ...] | None:
    if table is None:
        return None
    rows = table.list("sets", nonempty=True)
    table.done()
    sets: list[ScreenshotSet] = []
    for index, row in enumerate(rows):
        entry = _Table(row, f"{table.key('sets')}[{index}]")
        display = entry.enum("display_type", IOS_DISPLAY_TYPES)
        directory = entry.path("dir")
        required = entry.bool("required")
        entry.done()
        if display in {s.display_type for s in sets}:
            raise ConfigError(f"{entry.key('display_type')}: {display} is listed twice")
        if display == "WATCH":
            if required:
                raise ConfigError(f"{entry.key('required')}: the WATCH set must have required = false")
        elif not required:
            raise ConfigError(f"{entry.key('required')}: non-WATCH sets must have required = true in this version")
        sets.append(ScreenshotSet(display, directory, required))
    watch = [i for i, s in enumerate(sets) if s.display_type == "WATCH"]
    if watch and watch[0] != len(sets) - 1:
        raise ConfigError(f"{table.key('sets')}: the WATCH set must come last")
    if not [s for s in sets if s.display_type != "WATCH"]:
        raise ConfigError(f"{table.key('sets')}: at least one iPhone or iPad set is needed")
    return tuple(sets)


def _creative_sizes(table: _Table) -> tuple[tuple[str, int], ...]:
    key = table.key("sizes")
    raw = table.raw("sizes")
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f'{key}: expected a non-empty table of "<slot>/<file>" = <bytes>')
    pins: list[tuple[str, int]] = []
    slots: set[str] = set()
    for relative, size in raw.items():
        entry = f'{key}."{relative}"'
        check_path(relative, entry)
        parts = relative.split("/")
        if len(parts) != 2 or parts[0] not in CREATIVE_SLOT_SUFFIXES:
            raise ConfigError(f"{entry}: must be <slot>/<file> with the slot {' or '.join(CREATIVE_SLOT_SUFFIXES)}")
        slot, name = parts
        if not name.lower().endswith(CREATIVE_SLOT_SUFFIXES[slot]):
            raise ConfigError(f"{entry}: the {slot} slot takes {', '.join(CREATIVE_SLOT_SUFFIXES[slot])} files")
        if slot in slots:
            raise ConfigError(f"{entry}: the {slot} slot takes one image")
        slots.add(slot)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ConfigError(f"{entry}: expected the file's size in bytes (a positive integer), got {size!r}")
        pins.append((relative, size))
    return tuple(sorted(pins))


def _ios(table: _Table, slug: str) -> Ios:
    reserved = IOS_STAGE_JOBS + RESERVED_JOB_NAMES
    tag_prefix = table.str("tag_prefix", pattern=TAG_PREFIX)
    bundle_id = table.str("bundle_id", pattern=BUNDLE_ID)
    asc_app_id = table.str("asc_app_id", pattern=ASC_APP_ID)
    team_id = table.str("team_id", pattern=TEAM_ID)
    project = table.path("project", suffix=".xcodeproj")
    scheme = table.str("scheme", pattern=SCHEME)
    app_target = table.str("app_target", pattern=SCHEME)
    info_plist = table.path("info_plist")
    locale = table.str("locale", "en-US", pattern=LOCALE)
    export_compliance = None
    if table.has("export_compliance"):
        export_compliance = table.enum("export_compliance", EXPORT_COMPLIANCE_ANSWERS)
    gate_jobs = _gate_jobs(table, reserved)

    build_table = table.table("build")
    if build_table is None:
        raise ConfigError(f"{table.key('build')}: required table is missing")
    developer_dir = check_abs_path(build_table.raw("developer_dir"), build_table.key("developer_dir"))
    if "beta" in developer_dir.lower():
        raise ConfigError(f"{build_table.key('developer_dir')}: App Store builds need a released Xcode, not {developer_dir}")
    checkout = _patterns(build_table, "checkout")
    cache_files = tuple(
        check_glob(value, f"{build_table.key('package_cache_files')}[{i}]")
        for i, value in enumerate(build_table.list("package_cache_files", ["**/Package.resolved"], nonempty=True))
    )
    pre_archive = _hooks(build_table, "pre_archive")
    build_table.done()
    build = IosBuild(developer_dir, checkout, cache_files, pre_archive)

    listing = _listing(table.table("listing"), "APPSTORE.md")
    whats_new = _whats_new(table.table("whats_new"), listing.markdown if listing else "APPSTORE.md")
    screenshots = _screenshot_sets(table.table("screenshots"))
    creative_table = table.table("creative")
    creative_dir = None
    creative_sizes = None
    if creative_table is not None:
        creative_dir = creative_table.path("dir", "store/creative/ios")
        if creative_table.has("sizes"):
            creative_sizes = _creative_sizes(creative_table)
        creative_table.done()
    products_table = table.table("products")
    iap_route = None
    if products_table is not None:
        iap_route = products_table.enum("iap_route", ("submission",), "submission")
        products_table.done()
    release_table = table.table("github_release")
    title = "iOS {version} (build {build})"
    if release_table is not None:
        title = check_template(release_table.raw("title", title), release_table.key("title"), IOS_TITLE_PLACEHOLDERS)
        release_table.done()
    table.done()
    return Ios(tag_prefix, bundle_id, asc_app_id, team_id, project, scheme, app_target, info_plist,
               locale, gate_jobs, build, listing, whats_new, screenshots, creative_dir, iap_route, title,
               export_compliance, creative_sizes)


def _android(table: _Table, slug: str) -> Android:
    reserved = ANDROID_STAGE_JOBS + RESERVED_JOB_NAMES
    tag_prefix = table.str("tag_prefix", pattern=TAG_PREFIX)
    package = table.str("package", pattern=PACKAGE)
    gradle_file = table.path("gradle_file", "android/app/build.gradle.kts")
    cert = table.str("upload_cert_sha256", pattern=CERT_SHA256)
    locale = table.str("locale", "en-US", pattern=LOCALE)
    gate_jobs = _gate_jobs(table, reserved)

    build_table = table.table("build")
    if build_table is None:
        raise ConfigError(f"{table.key('build')}: required table is missing")
    build_dir = build_table.path("dir", "android")
    tasks = build_table.list("tasks", [":app:bundleRelease", ":app:assembleRelease"], nonempty=True)
    for i, task in enumerate(tasks):
        if not isinstance(task, str) or not GRADLE_TASK.fullmatch(task):
            raise ConfigError(f"{build_table.key('tasks')}[{i}]: {task!r} is not a Gradle task name")
    aab = build_table.path("aab", f"{build_dir}/app/build/outputs/bundle/release/app-release.aab")
    apk = build_table.path("apk", f"{build_dir}/app/build/outputs/apk/release/app-release.apk")
    checkout = _patterns(build_table, "checkout")
    signing = build_table.enum("signing", ("keystore-properties",))
    keystore_properties = build_table.path("keystore_properties", f"{build_dir}/keystore.properties")
    build_table.done()
    build = AndroidBuild(build_dir, tuple(tasks), aab, apk, checkout, signing, keystore_properties)

    listing = _listing(table.table("listing"), "PLAYSTORE.md")
    whats_new = _whats_new(table.table("whats_new"), "PLAYSTORE.md")
    screenshots_table = table.table("screenshots")
    screenshots_dir = None
    pad = True
    if screenshots_table is not None:
        screenshots_dir = screenshots_table.path("dir")
        pad = screenshots_table.bool("pad_to_9_16", True)
        if pad is not True:
            raise ConfigError(f"{screenshots_table.key('pad_to_9_16')}: only true is supported by this version")
        screenshots_table.done()
    release_table = table.table("github_release")
    asset_name = "{slug}-v{version}"
    if release_table is not None:
        asset_name = check_template(release_table.raw("asset_name", asset_name), release_table.key("asset_name"),
                                    ASSET_NAME_PLACEHOLDERS)
        release_table.done()
    sample = render_template(asset_name, slug=slug, version="1.2.3", version_code="42")
    if not ASSET_NAME.fullmatch(sample):
        raise ConfigError(f"android.github_release.asset_name: {asset_name!r} renders to {sample!r}, "
                          "which is not a safe file name")
    table.done()
    return Android(tag_prefix, package, gradle_file, cert, locale, gate_jobs, build, listing, whats_new,
                   screenshots_dir, pad, asset_name)


def parse(data: dict[str, Any]) -> Config:
    root = _Table(data, "")
    schema = root.int("schema")
    if schema != SCHEMA:
        raise ConfigError(f"schema: {schema} is not supported by this store-release version (expected {SCHEMA})")

    app_table = root.table("app")
    if app_table is None:
        raise ConfigError("app: required table is missing")
    name = app_table.str("name")
    if not (1 <= len(name) <= 64) or not name.isprintable():
        raise ConfigError(f"app.name: {name!r} must be 1-64 printable characters")
    slug = app_table.str("slug", pattern=SLUG)
    app_table.done()

    notes_table = root.table("release_notes")
    release_notes = "RELEASE_NOTES.md"
    if notes_table is not None:
        release_notes = notes_table.path("path", release_notes)
        notes_table.done()

    catalog = None
    catalog_table = root.table("catalog")
    if catalog_table is not None:
        path = catalog_table.path("path")
        groups = catalog_table.list("groups", nonempty=True)
        for i, group in enumerate(groups):
            if not isinstance(group, str) or not GROUP.fullmatch(group):
                raise ConfigError(f"catalog.groups[{i}]: {group!r} does not match {GROUP.pattern}")
        if len(set(groups)) != len(groups):
            raise ConfigError("catalog.groups: a group is listed twice")
        id_prefix = catalog_table.str("id_prefix", pattern=ID_PREFIX)
        play_rules = catalog_table.bool("play_rules", False)
        revenuecat = catalog_table.path("revenuecat", None)
        checks = _hooks(catalog_table, "checks")
        catalog_table.done()
        catalog = Catalog(path, tuple(groups), id_prefix, play_rules, revenuecat, checks)

    checks_paths: tuple[str, ...] = ()
    checks_table = root.table("checks")
    if checks_table is not None:
        values = checks_table.list("paths", [])
        for i, value in enumerate(values):
            key = f"checks.paths[{i}]"
            if not isinstance(value, str) or not PATH_FILTER.fullmatch(value) or ".." in value.split("/"):
                raise ConfigError(f"{key}: {value!r} is not a safe path filter")
        checks_table.done()
        checks_paths = tuple(values)

    ios_table = root.table("ios")
    android_table = root.table("android")
    ios = _ios(ios_table, slug) if ios_table is not None else None
    android = _android(android_table, slug) if android_table is not None else None
    if ios is None and android is None:
        raise ConfigError("at least one of [ios] and [android] must be present")
    root.done()
    return Config(schema, App(name, slug), release_notes, catalog, checks_paths, ios, android)


def load(path: Path) -> Config:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    return parse(data)


def load_for_app(app_root: Path, config: str = DEFAULT_CONFIG) -> Config:
    check_path(config, "--config")
    return load(resolve_inside(app_root, config, "--config"))


# --- file checks (config check --files) -----------------------------------------


def _exists(app_root: Path, relative: str, key: str, *, kind: str = "any") -> Path:
    resolved = resolve_inside(app_root, relative, key)
    if kind == "file" and not resolved.is_file():
        raise ConfigError(f"{key}: {relative} is not a file in the app repository")
    if kind == "dir" and not resolved.is_dir():
        raise ConfigError(f"{key}: {relative} is not a folder in the app repository")
    if kind == "any" and not resolved.exists():
        raise ConfigError(f"{key}: {relative} does not exist in the app repository")
    return resolved


def check_files(config: Config, app_root: Path) -> list[str]:
    """Every file and folder the config names exists inside the app. Returns what was checked."""
    checked: list[str] = []

    def need(relative: str, key: str, kind: str) -> None:
        _exists(app_root, relative, key, kind=kind)
        checked.append(relative)

    if config.catalog is not None:
        need(config.catalog.path, "catalog.path", "file")
        if config.catalog.revenuecat:
            need(config.catalog.revenuecat, "catalog.revenuecat", "file")
        for i, hook in enumerate(config.catalog.checks):
            need(hook, f"catalog.checks[{i}]", "file")
    if config.ios is not None:
        ios = config.ios
        need(ios.project, "ios.project", "dir")
        need(ios.pbxproj, "ios.project", "file")
        need(ios.info_plist, "ios.info_plist", "file")
        for i, job in enumerate(ios.gate_jobs):
            need(job.uses[2:], f"ios.gate_jobs[{i}].uses", "file")
        for i, hook in enumerate(ios.build.pre_archive):
            need(hook, f"ios.build.pre_archive[{i}]", "file")
        if ios.listing:
            need(ios.listing.markdown, "ios.listing.markdown", "file")
        if ios.whats_new:
            need(ios.whats_new.markdown, "ios.whats_new.markdown", "file")
        for i, shot_set in enumerate(ios.screenshot_sets or ()):
            if shot_set.required:
                need(shot_set.dir, f"ios.screenshots.sets[{i}].dir", "dir")
            else:
                resolve_inside(app_root, shot_set.dir, f"ios.screenshots.sets[{i}].dir")
        if ios.creative_dir:
            need(ios.creative_dir, "ios.creative.dir", "dir")
        check_info_plist(config, app_root)
        checked.append(f"{ios.info_plist} (INFOPLIST_FILE)")
    if config.android is not None:
        android = config.android
        need(android.gradle_file, "android.gradle_file", "file")
        need(android.build.dir, "android.build.dir", "dir")
        for i, job in enumerate(android.gate_jobs):
            need(job.uses[2:], f"android.gate_jobs[{i}].uses", "file")
        if android.listing:
            need(android.listing.markdown, "android.listing.markdown", "file")
        if android.whats_new:
            need(android.whats_new.markdown, "android.whats_new.markdown", "file")
        if android.screenshots_dir:
            need(android.screenshots_dir, "android.screenshots.dir", "dir")
    return checked


def check_pinned(config: Config, app_root: Path) -> list[str]:
    """What the config pins about the app's own files still holds (config check --files and --pinned).

    fud-ai's unit tests pinned these facts about its real files on every pull request and
    iOS prepare run; here they are config values checked the same way:

    - ios.export_compliance: the answer every app-target configuration declares
      (ITSAppUsesNonExemptEncryption), so changing it is a reviewed config change.
    - ios.creative.sizes: the header and search results images, by slot, file name and
      byte size, plus the image checks the store step runs. App Store Connect matches
      images by name and size, so a renamed or re-encoded file would be uploaded and sent
      to App Review again.

    Returns one line per pin checked (none when the config pins nothing).
    """
    from store_release.ios import asc  # noqa: PLC0415 - avoids an import cycle
    from store_release.ios import release as ios_release  # noqa: PLC0415

    checked: list[str] = []
    ios = config.ios
    if ios is None:
        return checked
    if ios.export_compliance is not None:
        pbxproj = resolve_inside(app_root, ios.pbxproj, "ios.project")
        try:
            answer = ios_release.check_export_compliance(pbxproj, ios.app_target)
        except ios_release.ReleaseError as exc:
            raise ConfigError(f"ios.export_compliance: {exc}") from None
        if answer != ios.export_compliance:
            raise ConfigError(
                f"ios.export_compliance: the {ios.app_target} target declares {ios_release.ENCRYPTION_KEY} = "
                f"{answer}, but the config pins {ios.export_compliance}; change the pin only after reviewing "
                "the app's encryption"
            )
        checked.append(f"ios.export_compliance = {answer} in every {ios.app_target} configuration")
    if ios.creative_sizes is not None:
        assert ios.creative_dir is not None
        creative_dir = resolve_inside(app_root, ios.creative_dir, "ios.creative.dir")
        found: dict[str, int] = {}
        problems: list[str] = []
        for folder, placement_type, _label, formats in asc.CREATIVE_SLOTS:
            slot_dir = creative_dir / folder
            images = sorted(p for p in (slot_dir.iterdir() if slot_dir.is_dir() else [])
                            if p.is_file() and p.suffix.lower() in asc.CREATIVE_EXTENSIONS)
            for image in images:
                relative = f"{folder}/{image.name}"
                found[relative] = image.stat().st_size
                problem = asc.creative_problem(placement_type, image, formats)
                if problem:
                    problems.append(f"{relative} {problem}")
        pinned = dict(ios.creative_sizes)
        for relative, size in sorted(pinned.items()):
            if relative not in found:
                problems.append(f"{relative} is missing (pinned at {size} bytes)")
            elif found[relative] != size:
                problems.append(f"{relative} is {found[relative]} bytes, pinned at {size}")
        problems += [f"{relative} is not pinned" for relative in sorted(set(found) - set(pinned))]
        if problems:
            raise ConfigError(
                f"ios.creative.sizes: {ios.creative_dir} differs from the pinned images. App Store Connect "
                "matches images by file name and size, so a changed image would be uploaded and sent to "
                "App Review again; update the pin when that is intended:\n  " + "\n  ".join(problems)
            )
        checked.append(f"ios.creative.sizes: {len(pinned)} image(s) in {ios.creative_dir} as pinned")
    return checked


def check_info_plist(config: Config, app_root: Path) -> None:
    """ios.info_plist must equal every app-target configuration's resolved INFOPLIST_FILE."""
    from store_release.ios import release as ios_release  # noqa: PLC0415 - avoids an import cycle

    ios = config.ios
    assert ios is not None
    pbxproj = resolve_inside(app_root, ios.pbxproj, "ios.project")
    try:
        configs = ios_release.app_configurations(pbxproj, ios.app_target)
    except ios_release.ReleaseError as exc:
        raise ConfigError(f"ios.app_target: {exc}") from None
    project_parent = Path(ios.project).parent
    for name, settings in sorted(configs.items()):
        info_file = settings.get("INFOPLIST_FILE", "")
        relative = re.sub(r"^\$[({]SRCROOT[)}]/", "", info_file)
        resolved = (project_parent / relative).as_posix() if relative else ""
        if resolved != ios.info_plist:
            raise ConfigError(
                f"ios.info_plist: {ios.info_plist} is not the {ios.app_target} target's {name} "
                f"INFOPLIST_FILE ({info_file or 'not set'} -> {resolved or 'none'})"
            )
