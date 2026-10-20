"""The run.py commands of the ported flows: each one calls the flow's own script entry point
with the arguments and switch environment that app's workflow step used.

The reusable workflows run the same commands for every flow; the flow decides what they do,
exactly as each app's own ios-release.yml / android-release.yml did:

  ios gates | build-number | wait-build ...     the flow's ios_release.py
  ios check-inputs                              prepare's offline file checks for the enabled
                                                switches (the app's "--dry-run" step)
  ios store-step --create-version --version V   the flow's asc_release.py --create-version
  ios preflight --version V                     the flow's read-only App Store Connect preflight
  ios store-step --version V [--build-id I --build-number N]
                                                the flow's store step
  ios export-compliance --info-plist P          the archived app's export compliance answer
  ios project-version --setting S               a single-valued project setting
  ios export-listing ...                        the flow's GET-only listing exporter
  catalog check [--platform ios|android]        the flow's catalog validator
  android gates | check-version-code | verify | play-state | check-tracks ...
  android play-listing check|prepare|upload|whats-new ...
"""
from __future__ import annotations

import contextlib
import io
import os
import plistlib
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Mapping

from store_release.config import Config, ConfigError
from store_release.flows import flow_package


class FlowUsage(ConfigError):
    """A command or option this flow does not have."""


@contextlib.contextmanager
def argv(args: list[str]) -> Iterator[None]:
    saved = sys.argv
    sys.argv = args
    try:
        yield
    finally:
        sys.argv = saved


@contextlib.contextmanager
def environment(values: Mapping[str, str | None]) -> Iterator[None]:
    """Set (or, with None, remove) environment variables for one call."""
    saved = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def module(flow: str, name: str):
    import importlib  # noqa: PLC0415

    return importlib.import_module(f"{flow_package(flow).__name__}.{name}")


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


SWITCH_ENV = ("UPLOAD_LISTING", "UPLOAD_WHATS_NEW", "UPLOAD_SCREENSHOTS", "UPLOAD_CREATIVE_ASSETS",
              "SUBMIT_IOS_REVIEW", "PUBLIC_RELEASE")


def call_asc(flow: str, args: list[str]) -> None:
    asc = module(flow, "asc_release")
    if flow == "quit-all":
        asc.main(args)
        return
    with argv([f"{flow} asc_release.py", *args]):
        asc.main()


# --- iOS -------------------------------------------------------------------------


def ios(config: Config, sub: str, args: list[str]) -> int:
    flow = config.ios.flow
    release = module(flow, "ios_release")
    if sub in ("gates", "build-number", "wait-build"):
        release.main([sub, *args])
        return 0
    if sub == "export-compliance":
        return export_compliance(flow, release, args)
    if sub == "project-version":
        return project_version(flow, release, args)
    if sub == "check-inputs":
        return check_inputs(config, flow, args)
    if sub == "preflight":
        return preflight(config, flow, args)
    if sub == "store-step":
        return store_step(config, flow, args)
    if sub == "export-listing":
        return export_listing(flow, args)
    if sub == "pr-checks":
        return ios_pr_checks(config, flow, args)
    raise FlowUsage(f"the {flow} flow has no `ios {sub}` command")


def export_compliance(flow: str, release, args: list[str]) -> int:
    if flow in ("scowld", "abysm", "pcm-notes"):
        # The flow's own command (its options, --info-plist included, unchanged).
        release.main(["export-compliance", *args])
        return 0
    if len(args) != 2 or args[0] != "--info-plist":
        raise FlowUsage("ios export-compliance needs --info-plist PATH (the archived app's Info.plist)")
    # Verceltics and Quit All checked the archived Info.plist with PlistBuddy in their workflow
    # (verify_ios_bundles.sh for Quit All): a boolean, and YES only with a compliance code.
    plist_export_compliance(Path(args[1]))
    return 0


def plist_export_compliance(info_plist: Path) -> str:
    try:
        with info_plist.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {info_plist}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    value = info.get("ITSAppUsesNonExemptEncryption") if isinstance(info, dict) else None
    if value is False:
        print("export compliance: no non-exempt encryption (ITSAppUsesNonExemptEncryption = NO)")
        return "NO"
    if value is True:
        if not str(info.get("ITSEncryptionExportComplianceCode") or ""):
            print("error: the app declares non-exempt encryption without ITSEncryptionExportComplianceCode",
                  file=sys.stderr)
            raise SystemExit(1)
        print("export compliance: non-exempt encryption with an export compliance code")
        return "YES"
    print("error: the app does not declare ITSAppUsesNonExemptEncryption (export compliance)", file=sys.stderr)
    raise SystemExit(1)


def project_value(flow: str, setting: str) -> str:
    release = module(flow, "ios_release")
    if flow == "abysm":
        reader = {"MARKETING_VERSION": release.project_marketing_version,
                  "CURRENT_PROJECT_VERSION": release.project_build_number}.get(setting)
        if reader is None:
            raise FlowUsage(f"the abysm flow reads only MARKETING_VERSION and CURRENT_PROJECT_VERSION, not {setting}")
        return str(reader())
    return str(release.single_project_value(setting))


def project_version(flow: str, release, args: list[str]) -> int:
    if len(args) != 2 or args[0] != "--setting":
        raise FlowUsage("ios project-version needs --setting NAME")
    try:
        print(project_value(flow, args[1]))
    except release.ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def check_inputs(config: Config, flow: str, args: list[str]) -> int:
    """prepare's offline check of the files the enabled switches use (no network)."""
    if args:
        raise FlowUsage("ios check-inputs takes no arguments; it reads the switch environment")
    asc_on = truthy("GATES_ASC")
    if flow == "scowld":
        if not asc_on:
            print("check-inputs: no App Store Connect switch is on")
            return 0
        # Scowld checked the four file switches only (review needs no local file).
        with environment({"SUBMIT_IOS_REVIEW": None, "PUBLIC_RELEASE": None}):
            call_asc(flow, ["--dry-run"])
        return 0
    if flow == "verceltics":
        metadata = module(flow, "ios_metadata")
        listing, screenshots = truthy("UPLOAD_LISTING"), truthy("UPLOAD_SCREENSHOTS")
        if listing or screenshots:
            flags = [flag for flag, on in (("--listing", listing), ("--screenshots", screenshots)) if on]
            metadata.main(["check", *flags])
        if truthy("UPLOAD_WHATS_NEW"):
            with contextlib.redirect_stdout(io.StringIO()):
                metadata.main(["whats-new", "--tag", os.environ.get("GITHUB_REF_NAME", "")])
            print("What's New: the tag's release notes are usable")
        if truthy("UPLOAD_CREATIVE_ASSETS"):
            only_creative = {name: None for name in SWITCH_ENV}
            only_creative["UPLOAD_CREATIVE_ASSETS"] = "true"
            with environment(only_creative):
                call_asc(flow, ["--dry-run"])
        return 0
    if flow == "abysm":
        # ABYSM ran this check on every run, its switches off or not.
        call_asc(flow, ["--dry-run", "--build-id", "DRY-RUN", "--build-number", "1"])
        return 0
    if flow == "pcm-notes":
        if not asc_on:
            print("check-inputs: no App Store Connect switch is on")
            return 0
        call_asc(flow, ["--dry-run", "--build-id", "pending", "--build-number", "pending"])
        return 0
    if flow == "quit-all":
        if not asc_on:
            print("check-inputs: no App Store Connect switch is on")
            return 0
        call_asc(flow, ["--dry-run", "--version", os.environ.get("MARKETING_VERSION", ""), "--build-id", "CHECK",
                        "--build-number", "0"])
        return 0
    raise FlowUsage(f"unknown flow {flow}")


@contextlib.contextmanager
def verceltics_metadata(tag_whats_new: bool) -> Iterator[Path]:
    """ios_metadata.py prepare into a temporary folder: the listing (with UPLOAD_LISTING) and the
    tag's What's New (with UPLOAD_WHATS_NEW), as Verceltics' preflight and store step read them."""
    metadata = module("verceltics", "ios_metadata")
    with tempfile.TemporaryDirectory(prefix="store-meta-") as out:
        prep = ["prepare", "--out", out]
        if tag_whats_new:
            prep += ["--tag", os.environ.get("GITHUB_REF_NAME", "")]
        if not truthy("UPLOAD_LISTING"):
            prep += ["--no-listing"]
        metadata.main(prep)
        yield Path(out)


def preflight(config: Config, flow: str, args: list[str]) -> int:
    if len(args) != 2 or args[0] != "--version":
        raise FlowUsage("ios preflight needs --version V")
    version = args[1]
    if flow in ("scowld", "abysm", "quit-all"):
        call_asc(flow, ["--preflight", "--version", version])
        return 0
    if flow == "verceltics":
        with verceltics_metadata(truthy("UPLOAD_WHATS_NEW")) as out:
            call_asc(flow, ["--preflight", "--version", version, "--metadata-dir", str(out)])
        return 0
    if flow == "pcm-notes":
        flags = [flag for flag, name in (("--listing", "UPLOAD_LISTING"), ("--whats-new", "UPLOAD_WHATS_NEW"),
                                         ("--screenshots", "UPLOAD_SCREENSHOTS"),
                                         ("--creative", "UPLOAD_CREATIVE_ASSETS"), ("--submit", "SUBMIT_IOS_REVIEW"))
                 if truthy(name)]
        module(flow, "ios_release").main(["preflight", "--version", version, *flags])
        return 0
    raise FlowUsage(f"unknown flow {flow}")


def store_step(config: Config, flow: str, args: list[str]) -> int:
    allowed = {"--version", "--build-id", "--build-number", "--create-version", "--dry-run"}
    for arg in args:
        if arg.startswith("--") and arg.split("=", 1)[0] not in allowed:
            raise FlowUsage(f"the {flow} flow's store step takes no {arg.split('=', 1)[0]} "
                            "(it reads the app's own store files)")
    if "--create-version" in args or "--dry-run" in args or flow != "verceltics":
        call_asc(flow, list(args))
        return 0
    with verceltics_metadata(truthy("UPLOAD_WHATS_NEW")) as out:
        call_asc(flow, [*args, "--metadata-dir", str(out)])
    return 0


def newest_section(config: Config, prefix: str) -> str | None:
    """The newest `## <prefix>X.Y` tag in the release notes (the tag PR checks prepare What's New for)."""
    import re  # noqa: PLC0415

    from store_release import release_notes  # noqa: PLC0415

    path = Path(release_notes.ROOT) / config.release_notes
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(rf"## ({re.escape(prefix)}[0-9]+(\.[0-9]+)*)\s*", line)
        if match:
            return match.group(1)
    return None


def creative_slots_committed(flow: str) -> None:
    """Verceltics' committed-asset test: both slot folders exist, each with at most one image that
    passes the local checks (test_store_assets.py test_repository_creative_images_pass_the_local_checks)."""
    asc = module(flow, "asc_release")
    for folder, placement_type, label, formats in asc.CREATIVE_SLOTS:
        slot = Path(asc.DEFAULT_CREATIVE_DIR) / folder
        if not slot.is_dir():
            print(f"error: {slot} is missing", file=sys.stderr)
            raise SystemExit(1)
        images = [p for p in slot.iterdir() if p.is_file() and p.suffix.lower() in asc.CREATIVE_EXTENSIONS]
        if len(images) > 1:
            print(f"error: the {label} slot takes one image", file=sys.stderr)
            raise SystemExit(1)
        for path in images:
            problem = asc.creative_problem(placement_type, path, formats)
            if problem:
                print(f"error: {path.name} {problem}", file=sys.stderr)
                raise SystemExit(1)


def ios_pr_checks(config: Config, flow: str, args: list[str]) -> int:
    """The committed store files, every switch on, as that app's pull request checks ran them."""
    if args:
        raise FlowUsage("ios pr-checks takes no arguments")
    off = {name: None for name in SWITCH_ENV}
    if flow == "scowld":
        with environment({**off, "UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "UPLOAD_SCREENSHOTS": "true"}):
            call_asc(flow, ["--dry-run"])
        return 0
    if flow == "verceltics":
        metadata = module(flow, "ios_metadata")
        metadata.main(["check"])
        creative_slots_committed(flow)
        tag = newest_section(config, config.ios.tag_prefix)
        with tempfile.TemporaryDirectory(prefix="store-meta-") as tmp:
            out, notes_only = Path(tmp) / "store-meta", Path(tmp) / "store-meta-whats-new"
            metadata.main(["prepare", "--out", str(out), *(["--tag", tag] if tag else [])])
            # Screenshots are staged as RGB copies below (Pillow); this run checks the text.
            with environment({**off, "UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true" if tag else None,
                              "SUBMIT_IOS_REVIEW": "true"}):
                call_asc(flow, ["--dry-run", "--metadata-dir", str(out), "--build-id", "DRY-RUN", "--build-number", "1"])
            if tag:
                # With IOS_UPLOAD_LISTING off a release prepares only What's New and never reads the listing.
                metadata.main(["prepare", "--out", str(notes_only), "--tag", tag, "--no-listing"])
                with environment({**off, "UPLOAD_WHATS_NEW": "true"}):
                    call_asc(flow, ["--dry-run", "--metadata-dir", str(notes_only)])
        with environment({**off, "UPLOAD_SCREENSHOTS": "true"}):
            call_asc(flow, ["--dry-run"])
        return 0
    if flow == "abysm":
        with environment({**off, "UPLOAD_LISTING": "true", "UPLOAD_SCREENSHOTS": "true",
                          "UPLOAD_CREATIVE_ASSETS": "true", "SUBMIT_IOS_REVIEW": "true", "PUBLIC_RELEASE": "false"}):
            call_asc(flow, ["--dry-run", "--build-id", "DRY-RUN", "--build-number", "1"])
        return 0
    if flow == "pcm-notes":
        with environment({**off, "UPLOAD_LISTING": "true", "UPLOAD_SCREENSHOTS": "true", "SUBMIT_IOS_REVIEW": "true",
                          "PUBLIC_RELEASE": "false"}):
            call_asc(flow, ["--dry-run", "--build-id", "DRY-RUN", "--build-number", "1"])
        asc = module(flow, "asc_release")
        creative = Path(asc.DEFAULT_CREATIVE_DIR)
        if any(p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg") for p in creative.rglob("*")):
            with environment({**off, "UPLOAD_CREATIVE_ASSETS": "true"}):
                call_asc(flow, ["--dry-run"])
        return 0
    if flow == "quit-all":
        asc = module(flow, "asc_release")
        marker = Path(asc.METADATA_DIR) / "ios" / asc.WHATS_NEW_VERSION_FILE
        version = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        with environment({**off, "UPLOAD_LISTING": "true", "UPLOAD_WHATS_NEW": "true", "UPLOAD_SCREENSHOTS": "true",
                          "SUBMIT_IOS_REVIEW": "true", "PUBLIC_RELEASE": "false"}):
            call_asc(flow, ["--dry-run", "--version", version, "--build-id", "DRY-RUN", "--build-number", "1"])
        with environment({**off, "UPLOAD_CREATIVE_ASSETS": "true"}):
            call_asc(flow, ["--dry-run"])
        return 0
    raise FlowUsage(f"unknown flow {flow}")


def android_pr_checks(config: Config, flow: str, args: list[str]) -> int:
    """The committed Play listing and screenshots, as Verceltics' pull request checks ran them."""
    if args:
        raise FlowUsage("android pr-checks takes no arguments")
    listing = module(flow, "play_listing")
    listing.main(["check"])
    # test_store_assets.py: every Play field is present in every locale.
    for locale in listing.locales():
        values = listing.load_locale(listing.LISTING_DIR / locale)
        if set(values) != set(listing.FIELDS):
            print(f"error: the {locale} Play listing lacks {', '.join(sorted(set(listing.FIELDS) - set(values)))}",
                  file=sys.stderr)
            raise SystemExit(1)
    tag = newest_section(config, config.android.tag_prefix)
    with tempfile.TemporaryDirectory(prefix="store-meta-") as tmp:
        out = Path(tmp) / "store-meta"
        listing.main(["prepare", "--out", str(out), *(["--tag", tag] if tag else [])])
        with environment({"UPLOAD_LISTING": "true", "UPLOAD_SCREENSHOTS": "true"}):
            listing.main(["upload", "--dry-run", "--metadata-dir", str(out)])
    return 0


EXPORTERS = {"scowld": "asc_export_listing", "verceltics": "export_ios_listing", "abysm": "export_listing",
             "pcm-notes": "export_listing"}


def export_listing(flow: str, args: list[str]) -> int:
    name = EXPORTERS.get(flow)
    if name is None:
        raise FlowUsage(f"the {flow} flow has no listing exporter")
    exporter = module(flow, name)
    if flow == "scowld":
        return int(exporter.main(args) or 0)
    with argv([f"{flow} {name}.py", *args]):
        exporter.main()
    return 0


# --- catalog -----------------------------------------------------------------------


# The flows whose prepare checked the catalog only on a tag run (PCM Notes' release notes and
# catalog step, Quit All's committed-input tests); the others checked it on every run.
CATALOG_ON_TAG_RUNS_ONLY = ("pcm-notes", "quit-all")


def catalog_check(config: Config, args: list[str]) -> int:
    """The flow's catalog validator, as each app's prepare / release job / PR checks ran it.

    --event is the run's event: a manual validation dispatch skips the check for the flows
    that ran it only on tag runs; pull requests and pushes always run it.
    """
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="catalog check")
    parser.add_argument("--platform", choices=("ios", "android", "all"), default="all")
    parser.add_argument("--event", default="")
    ns = parser.parse_args(args)
    platform = ns.platform
    flow = (config.ios.flow if config.ios is not None and platform != "android" else None) or (
        config.android.flow if config.android is not None else "fud-ai")
    if ns.event == "workflow_dispatch" and flow in CATALOG_ON_TAG_RUNS_ONLY:
        print(f"catalog check: the {flow} flow checks the catalog on tag runs and pull requests")
        return 0
    if flow in ("abysm", "pcm-notes"):
        module(flow, "validate_catalog").main()
        return 0
    if flow == "verceltics":
        module(flow, "validate_catalog").main(["--platform", platform])
        return 0
    if flow == "quit-all":
        from store_release.flows.quit_all import catalog as quit_all_catalog  # noqa: PLC0415

        quit_all_catalog.check(config)
        return 0
    print(f"catalog check: the {flow} flow has no product catalog")
    return 0


# --- Android -----------------------------------------------------------------------


def android(config: Config, sub: str, args: list[str]) -> int:
    flow = config.android.flow
    if sub in ("gates", "check-version-code", "verify", "play-state", "check-tracks"):
        module(flow, "android_release").main([sub, *args])
        return 0
    if sub == "play-listing":
        module(flow, "play_listing").main(list(args))
        return 0
    if sub == "pr-checks":
        return android_pr_checks(config, flow, args)
    raise FlowUsage(f"the {flow} flow has no `android {sub}` command")
