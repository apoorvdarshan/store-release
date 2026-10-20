#!/usr/bin/env python3
"""Fuzz scripts/store/redact_logs.py: App Store Connect IDs never survive redaction.

Two properties, picked by the first input byte:

- Realistic IDs (a 10-character key ID and a UUID issuer ID) placed anywhere in
  arbitrary bytes, as UTF-8 or UTF-16 text: redact() leaves no copy of either.
- Arbitrary secret values: redact_tree() fails closed. Whatever it leaves in
  the folder holds no secret in any encoding, because a file it cannot clean is
  deleted.

  python3 fuzz/redact_logs_fuzz.py -max_total_time=60
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Mapping

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "store"))
with atheris.instrument_imports():
    import redact_logs

KEY_ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
UUID_CHARS = "0123456789abcdef"
NAMES = redact_logs.DEFAULT_NAMES


def check_realistic(data: bytes, secrets: Mapping[str, str]) -> None:
    clean = redact_logs.redact(data, secrets)
    if redact_logs.leaks(clean, secrets):
        raise AssertionError(f"an ID survived redaction: secrets={dict(secrets)!r} data={data!r}")


def check_fail_closed(folder: Path, data: bytes, secrets: Mapping[str, str]) -> None:
    log = folder / "build.log"
    log.write_bytes(data)
    redact_logs.redact_tree(folder, secrets)
    left = redact_logs.remaining_leaks(folder, secrets)
    if left:
        raise AssertionError(f"left unclean files {left}: secrets={dict(secrets)!r} data={data!r}")
    log.unlink(missing_ok=True)


def realistic_ids(fdp: atheris.FuzzedDataProvider) -> dict[str, str]:
    key_id = "".join(KEY_ID_CHARS[fdp.ConsumeIntInRange(0, len(KEY_ID_CHARS) - 1)] for _ in range(10))
    hex_digits = "".join(UUID_CHARS[fdp.ConsumeIntInRange(0, 15)] for _ in range(32))
    issuer = "-".join((hex_digits[:8], hex_digits[8:12], hex_digits[12:16], hex_digits[16:20], hex_digits[20:]))
    return {NAMES[0]: key_id, NAMES[1]: issuer}


def log_with(fdp: atheris.FuzzedDataProvider, secrets: Mapping[str, str]) -> bytes:
    """Arbitrary bytes with the secrets dropped in, each in a random encoding."""
    values = [value for value in secrets.values() if value]
    parts = []
    for _ in range(fdp.ConsumeIntInRange(0, 8)):
        parts.append(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64)))
        if values:
            value = fdp.PickValueInList(values)
            parts.append(value.encode(fdp.PickValueInList(list(redact_logs.ENCODINGS))))
    parts.append(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 256)))
    return b"".join(parts)


FOLDER = Path(tempfile.mkdtemp(prefix="redact-fuzz-"))


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        secrets = realistic_ids(fdp)
        check_realistic(log_with(fdp, secrets), secrets)
    else:
        secrets = {name: fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 12)) for name in NAMES}
        check_fail_closed(FOLDER, log_with(fdp, secrets), secrets)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
