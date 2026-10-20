#!/usr/bin/env python3
"""Fuzz the app config loader (store_release.config) and what is derived from it.

    python3 -I fuzz/fuzz_config.py [libFuzzer flags] [corpus dir]

store/store-release.toml comes from the app repository and steers what release jobs
check out and run. Properties:

- load and parse raise nothing but ConfigError, for any bytes and for any edit of a
  real config (keys removed or added, values of every TOML type, unsafe paths and
  templates);
- for every config they accept, each job's sparse-checkout patterns are rooted, one
  line each, without NUL or a '..' segment; the release title and asset name
  templates render; and context.configure() raises nothing but ConfigError;
- check_path, check_abs_path and check_sparse_pattern accept nothing absolute (or
  relative, for absolute paths), nothing with a '..' segment and nothing with a line
  break or NUL, and an accepted relative path resolves inside the app root;
  check_template accepts only templates that render with the allowed placeholders.

Input: the first byte picks raw TOML, an edited example config or one value check.
Seeds: examples/*/store-release.toml and tests/canary/*.toml (see fuzz/seeds.py).
"""
from __future__ import annotations

import copy
import datetime
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

import atheris  # noqa: E402

with atheris.instrument_imports():
    from store_release import config, context, paths

WORK = Path(tempfile.mkdtemp(prefix="fuzz-config-")).resolve()
APP_ROOT = WORK / "app"
APP_ROOT.mkdir()
CONFIG_FILE = WORK / "store-release.toml"
CONFIG_SOURCES = sorted(common.ROOT.glob("examples/*/store-release.toml")) + sorted(
    common.ROOT.glob("tests/canary/*.toml"))
BASES = [tomllib.loads(path.read_text(encoding="utf-8")) for path in CONFIG_SOURCES]
ENVIRON = {"GITHUB_WORKFLOW_REF": "owner/app/.github/workflows/ios-release.yml@refs/tags/v1.0",
           "GITHUB_REPOSITORY": "owner/app"}
TRICKY = ("", ".", "..", "/", "//", "../x", "x/..", "x/../y", "./x", "x/./y", "x//y", "/abs", "!/x", "!x", "/x/",
          "/x ", " /x", "x\\y", "x\x00y", "x\ny", "x\ry", "x\u2028y", "~/x", "*", "**/x", "x/*.png", "/../x",
          "!/../x", "{version}", "{build}", "{slug}-v{version}", "{}", "{0}", "{version!r}", "{version:>9}",
          "{version.real}", "{version[0]}", "{{", "}}", "{", "}", "{version}}", "x" * 300, "é/ü", "C:\\x",
          "true", "1", "NO", "YES", "v", "ios-v", "com.example.app", "APP_IPHONE_67", "WATCH", "/Applications/Xcode.app")


def key_names() -> list[str]:
    names: set[str] = {"unknown", "", "schema "}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                names.add(key)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for base in BASES:
        walk(base)
    return sorted(names)


KEYS = key_names()


def new_value(fdp: atheris.FuzzedDataProvider, old: object, depth: int = 0) -> object:
    kind = fdp.ConsumeIntInRange(0, 11)
    if kind == 0:
        return fdp.PickValueInList(TRICKY)
    if kind == 1:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 20))
    if kind == 2 and isinstance(old, str):
        fragment = fdp.PickValueInList(TRICKY)
        return fragment + old if fdp.ConsumeBool() else old + fragment
    if kind == 3:
        return fdp.PickValueInList((0, 1, -1, 2, 10, 2**63 - 1, -(2**63)))
    if kind == 4:
        return fdp.PickValueInList((0.0, 1.5, float("inf"), float("nan")))
    if kind == 5:
        return fdp.ConsumeBool()
    if kind == 6:
        return datetime.date(2026, 10, 21)
    if kind == 7 and depth < 2:
        return [new_value(fdp, None, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 3))]
    if kind == 8 and depth < 2:
        return {fdp.PickValueInList(KEYS): new_value(fdp, None, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 2))}
    if kind == 9:
        return [fdp.PickValueInList(TRICKY) for _ in range(fdp.ConsumeIntInRange(0, 3))]
    if kind == 10 and isinstance(old, list):
        return old + [copy.deepcopy(fdp.PickValueInList(old))] if old else []
    return fdp.PickValueInList(TRICKY)


def mutate(fdp: atheris.FuzzedDataProvider, data: dict) -> None:
    slots: list[tuple[dict | list, object]] = []
    containers: list[dict | list] = [data]

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key in list(node):
                slots.append((node, key))
                if isinstance(node[key], (dict, list)):
                    containers.append(node[key])
                walk(node[key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                slots.append((node, index))
                if isinstance(value, (dict, list)):
                    containers.append(value)
                walk(value)
    walk(data)
    operation = fdp.ConsumeIntInRange(0, 3)
    if operation == 0 and slots:
        container, key = fdp.PickValueInList(slots)
        del container[key]  # type: ignore[arg-type]
    elif operation == 1 and slots:
        container, key = fdp.PickValueInList(slots)
        container[key] = new_value(fdp, container[key])  # type: ignore[index]
    else:
        target = fdp.PickValueInList(containers)
        if isinstance(target, dict):
            target[fdp.PickValueInList(KEYS)] = new_value(fdp, None)
        else:
            target.append(new_value(fdp, None))


def check_accepted(parsed: config.Config) -> None:
    """What every accepted config must allow downstream."""
    for job in paths.JOBS:
        if (job.startswith("ios-") and parsed.ios is None) or (job.startswith("android-") and parsed.android is None):
            continue
        try:
            lines = paths.patterns(parsed, job, config_path=config.DEFAULT_CONFIG, environ=ENVIRON)
        except config.ConfigError:
            continue
        for line in lines:
            assert line.startswith(("/", "!/")), (job, line)
            assert not any(ch in line for ch in "\n\r\x00"), (job, line)
            assert ".." not in line.lstrip("!").split("/"), (job, line)
    if parsed.ios is not None:
        config.render_template(parsed.ios.title, **{name: "1" for name in config.IOS_TITLE_PLACEHOLDERS})
    if parsed.android is not None:
        config.render_template(parsed.android.asset_name, **{name: "1" for name in config.ASSET_NAME_PLACEHOLDERS})
    try:
        # The CLI configures right after loading; the ported flows check their own needs here.
        context.configure(parsed, APP_ROOT)
    except config.ConfigError:
        pass


def check_values(fdp: atheris.FuzzedDataProvider) -> None:
    if fdp.ConsumeBool():
        value = "".join(fdp.PickValueInList(TRICKY + ("a", "b/", "x.png")) for _ in range(fdp.ConsumeIntInRange(1, 4)))
    else:
        value = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 40))
    which = fdp.ConsumeIntInRange(0, 3)
    try:
        if which == 0:
            config.check_path(value, "k")
            assert not value.startswith("/") and not any(ch in value for ch in "\\\x00\n\r"), value
            assert all(part not in ("", ".", "..") for part in value.split("/")), value
            resolved = config.resolve_inside(APP_ROOT, value, "k")
            assert resolved == APP_ROOT or APP_ROOT in resolved.parents, (value, resolved)
        elif which == 1:
            config.check_abs_path(value, "k")
            assert value.startswith("/") and not any(ch in value for ch in "\\\x00\n\r"), value
            assert all(part not in (".", "..") for part in value.split("/")), value
        elif which == 2:
            config.check_sparse_pattern(value, "k")
            assert value.startswith(("/", "!/")) and not any(ch in value for ch in "\n\r\x00"), value
            assert ".." not in value.lstrip("!").split("/"), value
        else:
            allowed = fdp.PickValueInList((config.IOS_TITLE_PLACEHOLDERS, config.ASSET_NAME_PLACEHOLDERS))
            config.check_template(value, "k", allowed)
            config.render_template(value, **{name: "1" for name in allowed})
    except config.ConfigError:
        pass


def TestOneInput(data: bytes) -> None:
    if not data:
        return
    mode = data[0] % 3
    if mode == 0:
        CONFIG_FILE.write_bytes(data[1:])
        try:
            parsed = config.load(CONFIG_FILE)
        except config.ConfigError:
            return
        check_accepted(parsed)
        return
    fdp = atheris.FuzzedDataProvider(data[1:])
    if mode == 2:
        check_values(fdp)
        return
    document = copy.deepcopy(fdp.PickValueInList(BASES))
    for _ in range(fdp.ConsumeIntInRange(0, 4)):
        mutate(fdp, document)
    try:
        parsed = config.parse(document)
    except config.ConfigError:
        return
    check_accepted(parsed)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
