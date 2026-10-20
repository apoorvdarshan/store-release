"""run.py's command dispatcher.

    python3 -I -u .store-release/run.py [--app-root DIR] [--config PATH] <command> [args...]

--app-root is required by every command that reads the app (it is never the
tooling checkout); --config defaults to store/store-release.toml under it.
Exit codes: 0 success, 1 a release precondition failed (`error: ...` on
stderr), 2 a usage error. No command prints a secret value.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path
from typing import Iterator

from store_release import __version__
from store_release.config import DEFAULT_CONFIG, Config, ConfigError

TOOL_ROOT = Path(__file__).resolve().parents[1]

USAGE = """usage: run.py [--app-root DIR] [--config PATH] <command> [args...]

commands:
  selftest           run store-release's own tests (see selftest --help)
  tool check         the tooling checkout is this workflow's commit
  config check       load and validate the app's store-release.toml (--files: every named file exists)
  config export      write STORE_RELEASE_* for a job to $GITHUB_ENV
  paths              print a job's sparse-checkout patterns
  stage check        re-validate a stage's inputs and gate results
  secrets check      fail unless HAS_<NAME>=true for each secret name
  caller check       check the caller workflow (--runtime) or every caller (--pr)
  callers render     render an app's caller workflow from its config
  pins check         every store-release ref in the app's workflows is this version's SHA
  yanked check       refuse (or, with --warn-only, warn about) a yanked store-release version
  print-switches     print the store switches (never mutates a store)
  release-notes      the tag's reviewed release notes (or --check-headings)
  catalog check      the product catalog's generic rules, then the app's catalog hooks
  revenuecat-gate    count the catalog; fail closed when STORE_SYNC_REVENUECAT is on
  listing prepare    listing text from the listing docs (--out required)
  whats-new prepare  What's New from the listing docs (--out required)
  screenshots stage  stage screenshots for upload (--out required)
  hooks run          run an app's build hooks with a scrubbed environment
  github release     create or update the tag's GitHub Release
  github stage-android-assets
  smoke              import the store tooling and mint an ES256 JWT (no network)
  ios <command>      gates, build-number, version-preflight, export-compliance, wait-build,
                     project-version, store-step, select-xcode, verify-archive,
                     export-options, redact-logs, plan
  android <command>  gates, verify, check-tracks, play-preflight, github-preflight,
                     write-signing-properties, gradle-version, play-listing, plan
  canary             store-release CI only: the checks sequence against a real app
"""


class UsageError(Exception):
    pass


@contextlib.contextmanager
def patched_argv(argv: list[str]) -> Iterator[None]:
    saved = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = saved


def split_global(argv: list[str]) -> tuple[Path | None, str, str, list[str]]:
    app_root: Path | None = None
    # config export writes STORE_RELEASE_CONFIG, so a job's later steps read the config its caller named.
    config = os.environ.get("STORE_RELEASE_CONFIG") or DEFAULT_CONFIG
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        option = argv[i]
        if option in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        if option == "--version":
            print(f"store-release {__version__}")
            raise SystemExit(0)
        name, eq, value = option.partition("=")
        if name not in ("--app-root", "--config"):
            raise UsageError(f"unknown option {option}")
        if not eq:
            if i + 1 >= len(argv):
                raise UsageError(f"{name} needs a value")
            value = argv[i + 1]
            i += 1
        if name == "--app-root":
            app_root = Path(value)
        else:
            config = value
        i += 1
    if i >= len(argv):
        raise UsageError("no command given")
    return app_root, config, argv[i], argv[i + 1:]


class App:
    """The app checkout, its loaded config, and the modules configured for it."""

    def __init__(self, app_root: Path | None, config_path: str) -> None:
        if app_root is None:
            raise UsageError("this command reads the app: pass --app-root DIR")
        if not app_root.is_dir():
            raise ConfigError(f"--app-root {app_root} is not a folder")
        self.root = app_root
        self.config_path = config_path
        from store_release import config as config_mod  # noqa: PLC0415

        self.config: Config = config_mod.load_for_app(app_root, config_path)
        from store_release import context  # noqa: PLC0415

        context.configure(self.config, app_root, config_path=config_path)

    def need(self, platform: str) -> None:
        if platform == "ios" and self.config.ios is None:
            raise ConfigError(f"{self.config_path} has no [ios]; the iOS commands need it")
        if platform == "android" and self.config.android is None:
            raise ConfigError(f"{self.config_path} has no [android]; the Android commands need it")

    def text_platform(self, requested: str) -> str:
        """--platform all means the platforms this app has (ios, play or all)."""
        if requested != "all":
            self.need("ios" if requested == "ios" else "android")
            return requested
        has_ios, has_android = self.config.ios is not None, self.config.android is not None
        return "all" if has_ios and has_android else ("ios" if has_ios else "play")


def option_value(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def replace_platform(args: list[str], app: App) -> list[str]:
    requested = option_value(args, "--platform") or "all"
    platform = app.text_platform(requested)
    out = []
    skip = False
    for i, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--platform":
            skip = True
            continue
        if arg.startswith("--platform="):
            continue
        out.append(arg)
    return ["--platform", platform, *out]


def two_words(command: str, rest: list[str], allowed: tuple[str, ...]) -> tuple[str, list[str]]:
    if not rest or rest[0] not in allowed:
        raise UsageError(f"{command} takes one of: {', '.join(allowed)}")
    return rest[0], rest[1:]


def run(argv: list[str]) -> int:
    app_root, config_path, command, rest = split_global(argv)

    if command == "selftest":
        from store_release import selftest  # noqa: PLC0415

        return selftest.main(rest)
    if command == "smoke":
        from store_release import smoke  # noqa: PLC0415

        smoke.main()
        return 0
    if command == "print-switches":
        from store_release import switches  # noqa: PLC0415

        switches.main(rest)
        return 0
    if command == "tool":
        sub, args = two_words(command, rest, ("check",))
        parser = argparse.ArgumentParser(prog="tool check")
        parser.add_argument("--tool-sha", required=True)
        parser.add_argument("--expected", default="")
        ns = parser.parse_args(args)
        from store_release import stage  # noqa: PLC0415

        stage.tool_check(TOOL_ROOT, ns.tool_sha, ns.expected, job=os.environ.get("SETUP_JOB", ""))
        return 0
    if command == "secrets":
        sub, args = two_words(command, rest, ("check",))
        if not args:
            raise UsageError("secrets check needs at least one secret name")
        from store_release import stage  # noqa: PLC0415

        stage.secrets_check(args)
        return 0
    if command == "yanked":
        sub, args = two_words(command, rest, ("check",))
        parser = argparse.ArgumentParser(prog="yanked check")
        parser.add_argument("--warn-only", action="store_true")
        ns = parser.parse_args(args)
        from store_release import yanked  # noqa: PLC0415

        yanked.check(warn_only=ns.warn_only)
        return 0
    if command == "ios" and rest[:1] == ["redact-logs"]:
        # A failure() step: it must work even when the app or its config could not be read.
        from store_release import redact  # noqa: PLC0415

        return redact.main(["ios redact-logs", *rest[1:]])
    if command == "callers":
        sub, args = two_words(command, rest, ("render",))
        from store_release import callers  # noqa: PLC0415

        app = App(app_root, config_path)
        return callers.render_main(app.config, args, config_path=config_path)

    app = App(app_root, config_path)

    if command == "config":
        sub, args = two_words(command, rest, ("check", "export"))
        from store_release import config as config_mod  # noqa: PLC0415

        if sub == "check":
            parser = argparse.ArgumentParser(prog="config check")
            parser.add_argument("--files", action="store_true")
            ns = parser.parse_args(args)
            platforms = [name for name, value in (("ios", app.config.ios), ("android", app.config.android)) if value]
            print(f"{config_path}: schema {app.config.schema}, {app.config.app.name} ({', '.join(platforms)})")
            if ns.files:
                checked = config_mod.check_files(app.config, app.root)
                print(f"every file and folder the config names exists ({len(checked)} checked)")
            return 0
        parser = argparse.ArgumentParser(prog="config export")
        parser.add_argument("--job", required=True)
        ns = parser.parse_args(args)
        from store_release import stage  # noqa: PLC0415

        stage.config_export(app.config, ns.job, config_path=config_path)
        return 0
    if command == "paths":
        parser = argparse.ArgumentParser(prog="paths")
        parser.add_argument("--job", required=True)
        ns = parser.parse_args(rest)
        from store_release import paths  # noqa: PLC0415

        for line in paths.patterns(app.config, ns.job, config_path=config_path):
            print(line)
        return 0
    if command == "stage":
        sub, args = two_words(command, rest, ("check",))
        from store_release import stage  # noqa: PLC0415

        parser = argparse.ArgumentParser(prog="stage check")
        stage.add_stage_arguments(parser)
        ns = parser.parse_args(args)
        stage.stage_check(ns.stage, ns, app.config)
        return 0
    if command in ("caller", "pins"):
        from store_release import callers  # noqa: PLC0415

        if command == "pins":
            two_words(command, rest, ("check",))
            callers.pins_check(app.root)
            return 0
        sub, args = two_words(command, rest, ("check",))
        parser = argparse.ArgumentParser(prog="caller check")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--runtime", action="store_true")
        mode.add_argument("--pr", action="store_true")
        parser.add_argument("--platform", choices=("ios", "android"))
        ns = parser.parse_args(args)
        if ns.runtime:
            if not ns.platform:
                raise UsageError("caller check --runtime needs --platform ios|android")
            app.need(ns.platform)
            callers.runtime_check(app.root, ns.platform)
        else:
            callers.pr_check(app.config, app.root, config_path=config_path)
        return 0
    if command == "release-notes":
        from store_release import release_notes  # noqa: PLC0415

        release_notes.main(rest)
        return 0
    if command == "catalog":
        two_words(command, rest, ("check",))
        from store_release import catalog  # noqa: PLC0415

        catalog.check()
        return 0
    if command == "revenuecat-gate":
        from store_release import catalog  # noqa: PLC0415

        catalog.revenuecat_gate()
        return 0
    if command in ("listing", "whats-new", "screenshots"):
        verb = {"listing": "prepare", "whats-new": "prepare", "screenshots": "stage"}[command]
        sub, args = two_words(command, rest, (verb,))
        if option_value(args, "--out") is None:
            raise UsageError(f"{command} {verb} needs --out DIR (under $RUNNER_TEMP, never the app checkout)")
        args = replace_platform(args, app)
        if command == "listing":
            from store_release.text import listing as module  # noqa: PLC0415
        elif command == "whats-new":
            from store_release.text import whats_new as module  # noqa: PLC0415
        else:
            from store_release import screenshots as module  # noqa: PLC0415
        with patched_argv([f"{command} {verb}", *args]):
            module.main()
        return 0
    if command == "hooks":
        sub, args = two_words(command, rest, ("run",))
        parser = argparse.ArgumentParser(prog="hooks run")
        parser.add_argument("--stage", required=True, choices=("pre_archive",))
        parser.parse_args(args)
        app.need("ios")
        from store_release import hooks  # noqa: PLC0415

        extra = {name: os.environ[name] for name in ("MARKETING_VERSION", "BUILD_NUMBER", "DEVELOPER_DIR")
                 if name in os.environ}
        hooks.run(app.root, app.config.ios.build.pre_archive, env_extra=extra)
        if not app.config.ios.build.pre_archive:
            print("no ios.build.pre_archive hooks")
        return 0
    if command == "github":
        sub, args = two_words(command, rest, ("release", "stage-android-assets"))
        from store_release import github  # noqa: PLC0415

        if sub == "stage-android-assets":
            app.need("android")
            parser = argparse.ArgumentParser(prog="github stage-android-assets")
            parser.add_argument("--tag", required=True)
            parser.add_argument("--out", required=True, type=Path)
            ns = parser.parse_args(args)
            github.stage_android_assets(app.config, app.root, tag=ns.tag, out=ns.out)
            return 0
        parser = argparse.ArgumentParser(prog="github release")
        parser.add_argument("--platform", required=True, choices=("ios", "android"))
        parser.add_argument("--tag", required=True)
        parser.add_argument("--notes-file", default="")
        parser.add_argument("--version", default="")
        parser.add_argument("--uploaded-build", default="")
        parser.add_argument("--production", default="")
        parser.add_argument("--assets-dir", default="")
        ns = parser.parse_args(args)
        app.need(ns.platform)
        github.release(app.config, app.root, platform=ns.platform, tag=ns.tag, notes_file=ns.notes_file,
                       version=ns.version, uploaded_build=ns.uploaded_build, production=ns.production,
                       assets_dir=ns.assets_dir)
        return 0
    if command == "ios":
        app.need("ios")
        return ios_command(app, rest)
    if command == "android":
        app.need("android")
        return android_command(app, rest)
    if command == "canary":
        from store_release import canary  # noqa: PLC0415

        return canary.main(app, rest)
    raise UsageError(f"unknown command {command!r}")


IOS_RELEASE_COMMANDS = ("gates", "build-number", "version-preflight", "export-compliance", "wait-build",
                        "project-version")
IOS_ARCHIVE_COMMANDS = ("select-xcode", "verify-archive", "export-options")


def ios_command(app: App, rest: list[str]) -> int:
    if not rest:
        raise UsageError("ios needs a command")
    sub, args = rest[0], rest[1:]
    if sub in IOS_RELEASE_COMMANDS:
        from store_release.ios import release  # noqa: PLC0415

        release.main([sub, *args])
        return 0
    if sub == "store-step":
        from store_release.ios import asc  # noqa: PLC0415

        with patched_argv(["ios store-step", *args]):
            asc.main()
        return 0
    if sub in IOS_ARCHIVE_COMMANDS:
        from store_release.ios import archive  # noqa: PLC0415

        ios = app.config.ios
        archive.main([sub, *args], app_root=app.root, developer_dir=ios.build.developer_dir,
                     patterns=ios.build.package_cache_files, bundle_id=ios.bundle_id, team_id=ios.team_id)
        return 0
    if sub == "plan":
        from store_release.ios import plan  # noqa: PLC0415

        return plan.main(app, args)
    raise UsageError(f"unknown ios command {sub!r}")


ANDROID_RELEASE_COMMANDS = ("gates", "verify", "check-tracks", "play-preflight", "github-preflight",
                            "write-signing-properties", "gradle-version")


def android_command(app: App, rest: list[str]) -> int:
    if not rest:
        raise UsageError("android needs a command")
    sub, args = rest[0], rest[1:]
    if sub in ANDROID_RELEASE_COMMANDS:
        from store_release.android import release  # noqa: PLC0415

        release.main([sub, *args])
        return 0
    if sub == "play-listing":
        from store_release.android import play_listing  # noqa: PLC0415

        with patched_argv(["android play-listing", *args]):
            play_listing.main()
        return 0
    if sub == "plan":
        from store_release.android import plan  # noqa: PLC0415

        return plan.main(app, args)
    raise UsageError(f"unknown android command {sub!r}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    from store_release.config import ConfigError  # noqa: PLC0415
    from store_release.github import GithubError  # noqa: PLC0415
    from store_release.hooks import HookError  # noqa: PLC0415
    from store_release.ios.archive import ArchiveError  # noqa: PLC0415
    from store_release.stage import StageError  # noqa: PLC0415
    from store_release.yanked import YankedError  # noqa: PLC0415

    try:
        return run(argv)
    except UsageError as exc:
        print(f"error: {exc}\n\n{USAGE}", file=sys.stderr, end="")
        return 2
    except (ConfigError, StageError, HookError, GithubError, ArchiveError, YankedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
