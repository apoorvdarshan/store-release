#!/usr/bin/env python3
"""Redact App Store Connect key and issuer IDs from a folder before it is uploaded.

xcodebuild, altool and Xcode's distribution logs echo the API key ID and issuer
ID. Workflow artifacts are not masked like the console, and app repositories are
public, so every file is rewritten byte for byte (text and binary alike), any
file that still contains an ID afterwards, in any case or as UTF-16 text, is
deleted rather than uploaded, and a file or folder whose name holds an ID is
renamed (the union of every app's former redactor).

    ios redact-logs DIR     IDs come from ASC_KEY_ID and ASC_ISSUER_ID

Exits non-zero if a file cannot be cleaned or removed, so the caller can skip the
upload.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLACEHOLDERS = {"ASC_KEY_ID": b"<ASC_KEY_ID>", "ASC_ISSUER_ID": b"<ASC_ISSUER_ID>"}
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


def replace_ignoring_case(data: bytes, needle: bytes, placeholder: bytes) -> bytes:
    """Replace every ASCII-case-insensitive occurrence of needle in data."""
    lowered, target = data.lower(), needle.lower()
    out = bytearray()
    start = 0
    while True:
        index = lowered.find(target, start)
        if index < 0:
            out += data[start:]
            return bytes(out)
        out += data[start:index] + placeholder
        start = index + len(target)


def remaining_ids(data: bytes, ids: list[str]) -> bool:
    lowered = data.lower()
    return any(value.lower().encode(encoding) in lowered for value in ids for encoding in ENCODINGS)


def redact_folder(folder: Path, secrets: dict[str, str]) -> tuple[int, list[Path]]:
    """Redact every regular file under folder. Returns (files changed, files removed)."""
    ids = [value for value in secrets.values() if value]
    changed = 0
    removed: list[Path] = []
    if not ids or not folder.is_dir():
        return changed, removed
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            # Never follow a link out of the folder; links are not uploaded as data.
            path.unlink()
            removed.append(path)
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        cleaned = data
        for name, value in secrets.items():
            if value:
                cleaned = replace_ignoring_case(cleaned, value.encode("utf-8"), PLACEHOLDERS[name])
        if cleaned != data:
            path.write_bytes(cleaned)
            changed += 1
        if remaining_ids(cleaned, ids):
            # UTF-16 or otherwise encoded copies cannot be rewritten safely.
            path.unlink()
            removed.append(path)
    rename_ids(folder, secrets)
    return changed, removed


def redacted_name(name: str, secrets: dict[str, str]) -> str:
    """A file or folder name with every ID (any case) replaced by its placeholder's name."""
    for key, value in secrets.items():
        if value:
            name = replace_ignoring_case(name.encode("utf-8"), value.encode("utf-8"),
                                         PLACEHOLDERS[key].strip(b"<>")).decode("utf-8", "replace")
    return name


def rename_ids(folder: Path, secrets: dict[str, str]) -> list[Path]:
    """Rename files and folders whose name holds an ID (deepest first); an artifact's paths are
    as public as its contents. A name that cannot be renamed (the target exists) is removed."""
    renamed: list[Path] = []
    for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        clean = redacted_name(path.name, secrets)
        if clean == path.name:
            continue
        target = path.with_name(clean)
        if target.exists():
            if path.is_dir() and not path.is_symlink():
                import shutil  # noqa: PLC0415

                shutil.rmtree(path)
            else:
                path.unlink()
            continue
        path.rename(target)
        renamed.append(target)
    return renamed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    folder = Path(argv[1])
    secrets = {name: os.environ.get(name, "").strip() for name in PLACEHOLDERS}
    if not all(secrets.values()):
        print("redact: ASC_KEY_ID / ASC_ISSUER_ID not set; nothing to redact")
        return 0
    changed, removed = redact_folder(folder, secrets)
    print(f"redact: rewrote {changed} file(s)")
    for path in removed:
        print(f"redact: removed {path.relative_to(folder)} (could not be cleaned)")
    leftovers = [
        path
        for path in folder.rglob("*")
        if path.is_file() and remaining_ids(path.read_bytes(), list(secrets.values()))
    ] if folder.is_dir() else []
    if leftovers:
        print(f"error: {len(leftovers)} file(s) still contain a credential ID", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
