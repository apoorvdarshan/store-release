#!/usr/bin/env python3
"""Remove the App Store Connect key and issuer IDs from build logs before upload.

  ASC_KEY_ID=… ASC_ISSUER_ID=… python3 scripts/store/redact_logs.py <logs directory>

Workflow artifacts are not masked like the job log. In text files every
occurrence (any letter case) is replaced by <ASC_KEY_ID> / <ASC_ISSUER_ID>. Any
other file that still holds an ID, in UTF-8 or UTF-16 (a binary plist, a UTF-16
log), is deleted, as is anything whose name holds one and every symlink, so
nothing uploaded can carry the IDs. Read-only towards App Store Connect.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

PLACEHOLDERS = (("ASC_KEY_ID", b"<ASC_KEY_ID>"), ("ASC_ISSUER_ID", b"<ASC_ISSUER_ID>"))
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


def encoded_forms(secrets: list[str]) -> list[bytes]:
    """Every byte form an ID can take in a file: each case, UTF-8 and UTF-16."""
    variants = {case for secret in secrets for case in (secret, secret.lower(), secret.upper())}
    return [variant.encode(encoding) for variant in variants for encoding in ENCODINGS]


def redact(root: Path, replacements: dict[str, bytes]) -> tuple[int, int]:
    """Redact `root` in place. Returns (files rewritten, files or folders removed)."""
    replacements = {secret: placeholder for secret, placeholder in replacements.items() if secret}
    if not replacements:
        return 0, 0
    secrets = list(replacements)
    forms = encoded_forms(secrets)
    patterns = [
        (re.compile(re.escape(secret.encode("utf-8")), re.IGNORECASE), placeholder)
        for secret, placeholder in replacements.items()
    ]

    def named_after_a_secret(name: str) -> bool:
        return any(secret.lower() in name.lower() for secret in secrets)

    rewritten = removed = 0
    for folder, dirs, files in os.walk(root):
        for name in list(dirs):
            path = os.path.join(folder, name)
            if os.path.islink(path) or named_after_a_secret(name):
                if os.path.islink(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path)
                dirs.remove(name)
                removed += 1
        for name in files:
            path = os.path.join(folder, name)
            if os.path.islink(path) or named_after_a_secret(name):
                os.unlink(path)
                removed += 1
                continue
            with open(path, "rb") as handle:
                data = handle.read()
            if b"\0" not in data:  # text: keep it, minus the IDs
                cleaned = data
                for pattern, placeholder in patterns:
                    cleaned = pattern.sub(placeholder, cleaned)
                if cleaned != data:
                    with open(path, "wb") as handle:
                        handle.write(cleaned)
                    rewritten += 1
                    data = cleaned
            if any(form in data for form in forms):
                os.unlink(path)  # binary or UTF-16: cannot be rewritten safely
                removed += 1
    return rewritten, removed


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    root = Path(args[0])
    if not root.is_dir():
        print(f"{root} does not exist; nothing to redact")
        return
    replacements = {os.environ.get(name, "").strip(): placeholder for name, placeholder in PLACEHOLDERS}
    rewritten, removed = redact(root, replacements)
    print(f"redacted App Store Connect IDs in {rewritten} file(s); removed {removed} item(s) "
          "that could not be redacted")


if __name__ == "__main__":
    main()
