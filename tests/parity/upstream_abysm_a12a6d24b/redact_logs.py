#!/usr/bin/env python3
"""Redact App Store Connect IDs from build logs before they are kept as an artifact.

When ios-release.yml's build job fails it keeps the xcodebuild/altool logs and
Xcode's *.xcdistributionlogs. xcodebuild and altool echo the App Store Connect
key ID and issuer ID into them, and artifacts are not masked like the console.
Every value named on the command line (environment variables, by default
ASC_KEY_ID and ASC_ISSUER_ID) is replaced by <NAME> in every file, binary files
included, as UTF-8/ASCII and as UTF-16 (little- and big-endian) text.

Fails closed: a file that cannot be read, rewritten or verified clean is
deleted, and so is anything that is not a regular file (symlinks are never
followed). Exit status 0 means every file left under the folder is clean; any
other status means the folder must not be uploaded.

  python3 scripts/store/redact_logs.py "$RUNNER_TEMP/logs" ASC_KEY_ID ASC_ISSUER_ID
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping

ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
DEFAULT_NAMES = ("ASC_KEY_ID", "ASC_ISSUER_ID")


def forms(name: str, value: str) -> list[tuple[bytes, bytes]]:
    """(secret, placeholder) byte pairs, one per text encoding."""
    return [(value.encode(encoding), f"<{name}>".encode(encoding)) for encoding in ENCODINGS]


def redact(data: bytes, secrets: Mapping[str, str]) -> bytes:
    for name, value in secrets.items():
        if value:
            for secret, placeholder in forms(name, value):
                data = data.replace(secret, placeholder)
    return data


def leaks(data: bytes, secrets: Mapping[str, str]) -> bool:
    return any(
        secret in data for name, value in secrets.items() if value for secret, _ in forms(name, value)
    )


def remove(path: Path) -> bool:
    """Delete a file or link (never what a link points to); True when it is gone."""
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def redact_tree(root: Path, secrets: Mapping[str, str]) -> tuple[int, list[str], list[str]]:
    """Redact every file under root. Returns (files changed, removed, could not remove)."""
    changed = 0
    removed: list[str] = []
    stuck: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():  # os.walk lists but never enters these
                dirnames.remove(name)
                filenames.append(name)
        for name in filenames:
            path = here / name
            shown = str(path.relative_to(root))
            try:
                if path.is_symlink() or not path.is_file():
                    raise OSError("not a regular file")
                data = path.read_bytes()
                clean = redact(data, secrets)
                if clean != data:
                    path.write_bytes(clean)
                    changed += 1
                if leaks(path.read_bytes(), secrets):
                    raise OSError("still holds an ID after redaction")
            except OSError as exc:
                (removed if remove(path) else stuck).append(f"{shown} ({exc})")
    return changed, removed, stuck


def remaining_leaks(root: Path, secrets: Mapping[str, str]) -> list[str]:
    """Anything left under root that is not a clean regular file."""
    bad = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                bad.append(str(path.relative_to(root)))
            elif path.is_file():
                try:
                    if leaks(path.read_bytes(), secrets):
                        bad.append(str(path.relative_to(root)))
                except OSError:
                    bad.append(str(path.relative_to(root)))
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="folder that becomes the artifact")
    parser.add_argument("names", nargs="*", default=list(DEFAULT_NAMES),
                        help="environment variables whose values must not appear (default: %(default)s)")
    args = parser.parse_args(argv)
    secrets = {name: os.environ.get(name, "") for name in args.names}
    if not args.folder.is_dir():
        print(f"nothing to redact: {args.folder} is not a folder")
        return 0
    changed, removed, stuck = redact_tree(args.folder, secrets)
    for entry in removed:
        print(f"removed {entry}", file=sys.stderr)
    for entry in stuck:
        print(f"error: could not remove {entry}", file=sys.stderr)
    left = remaining_leaks(args.folder, secrets)
    for entry in left:
        print(f"error: {entry} is not clean", file=sys.stderr)
    if stuck or left:
        print("error: redaction incomplete; the logs must not be uploaded", file=sys.stderr)
        return 1
    print(f"redacted {changed} file(s); removed {len(removed)} that could not be redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
