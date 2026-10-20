#!/usr/bin/env python3
"""Make the iOS release failure logs safe to keep as a workflow artifact.

  redact_logs.py <logs dir>      (IDs come from ASC_KEY_ID and ASC_ISSUER_ID)

Artifacts are not masked like the console, and xcodebuild, altool and the
.xcdistributionlogs echo the App Store Connect key ID and issuer ID. Before the
folder is uploaded:

  * every copy of either ID in a file's bytes (UTF-8, any letter case) is
    replaced with <ASC_KEY_ID> / <ASC_ISSUER_ID>, in text and binary files alike;
  * files that are not plain UTF-8 text (binary plists, archives, UTF-16 logs,
    anything with NUL bytes) are removed, because an ID could hide there in an
    encoding the replacement does not cover;
  * a final scan removes any file that still holds an ID in UTF-8 or UTF-16,
    and file or folder names containing an ID are renamed.

Symbolic links are removed, never followed. Prints what it changed and exits 0;
the workflow deletes the whole folder if this script itself fails.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

LABELS = (("ASC_KEY_ID", "<ASC_KEY_ID>"), ("ASC_ISSUER_ID", "<ASC_ISSUER_ID>"))
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


def secrets_from_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Non-empty IDs to redact, keyed by their placeholder."""
    env = dict(os.environ) if env is None else env
    found: dict[str, str] = {}
    for name, placeholder in LABELS:
        value = (env.get(name) or "").strip()
        if value:
            found[placeholder] = value
    return found


def is_plain_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def redact_bytes(data: bytes, secrets: dict[str, str]) -> bytes:
    for placeholder, secret in secrets.items():
        pattern = re.compile(re.escape(secret.encode("utf-8")), re.IGNORECASE)
        data = pattern.sub(placeholder.encode("utf-8"), data)
    return data


def still_holds_secret(data: bytes, secrets: dict[str, str]) -> bool:
    lowered = data.lower()
    for secret in secrets.values():
        for encoding in ENCODINGS:
            if secret.lower().encode(encoding) in lowered:
                return True
    return False


def redact_name(name: str, secrets: dict[str, str]) -> str:
    for placeholder, secret in secrets.items():
        name = re.sub(re.escape(secret), placeholder.strip("<>"), name, flags=re.IGNORECASE)
    return name


def redact_tree(root: Path, secrets: dict[str, str]) -> dict[str, list[str]]:
    """Redact files under root in place. Returns relative paths per action."""
    report: dict[str, list[str]] = {"redacted": [], "removed": [], "renamed": []}

    def rel(path: Path) -> str:
        # Reported paths are redacted too: a folder name may still hold an ID.
        return redact_name(str(path.relative_to(root)), secrets)

    def remove(path: Path, why: str) -> None:
        report["removed"].append(f"{rel(path)} ({why})")
        path.unlink()

    def rename_if_needed(path: Path) -> None:
        new_name = redact_name(path.name, secrets)
        if new_name != path.name:
            target = path.with_name(new_name)
            # Report only the new name: the old one holds an ID.
            report["renamed"].append(f"{rel(target)} (its name held an ID)")
            path.rename(target)

    # Bottom-up and without following links: contents are handled before their
    # folder is renamed, and nothing outside root is ever read or deleted.
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if path.is_symlink():
                remove(path, "symbolic link")
                continue
            data = path.read_bytes()
            if not is_plain_text(data):
                remove(path, "not plain UTF-8 text")
                continue
            redacted = redact_bytes(data, secrets)
            if still_holds_secret(redacted, secrets):
                remove(path, "an ID could not be replaced")
                continue
            if redacted != data:
                path.write_bytes(redacted)
                report["redacted"].append(rel(path))
            rename_if_needed(path)
        for name in dirnames:
            path = base / name
            if path.is_symlink():
                remove(path, "symbolic link")
                continue
            rename_if_needed(path)
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: redact_logs.py <logs dir>", file=sys.stderr)
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"no logs at {root}; nothing to redact")
        return 0
    secrets = secrets_from_environment()
    if not secrets:
        print("ASC_KEY_ID and ASC_ISSUER_ID are empty; only removing files that are not plain text")
    report = redact_tree(root, secrets)
    for action in ("redacted", "renamed", "removed"):
        for entry in report[action]:
            print(f"  {action}: {entry}")
    print(
        f"logs ready: {len(report['redacted'])} redacted, {len(report['renamed'])} renamed, "
        f"{len(report['removed'])} removed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
