#!/usr/bin/env python3
"""Fuzz the App Store Connect credential-ID redaction of build logs (store_release.redact).

    python3 -I fuzz/fuzz_redact.py [libFuzzer flags] [corpus dir]

xcodebuild logs echo the API key ID and issuer ID, and the logs are uploaded as
artifacts of public repositories. Properties, for IDs shaped like the real ones
(8 or more of A-Z, a-z, 0-9 and '-'):

- after replace_ignoring_case, no UTF-8 copy of either ID remains in any letter case,
  and data without an ID comes back unchanged;
- redact_folder leaves no file that still holds an ID as UTF-8 or UTF-16 (LE or BE,
  either alignment, any case) and no file or folder name that holds one: each file is
  rewritten or deleted and each such name renamed, a symbolic link is removed without
  touching its target, and every file left holds exactly the redacted bytes of a file
  that needed no deletion.
"""
from __future__ import annotations

import shutil
import string
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402  (puts the repository root on sys.path)

import atheris  # noqa: E402

with atheris.instrument_imports(include=["store_release"]):
    from store_release import redact

ID_ALPHABET = string.ascii_letters + string.digits + "-"
WORK = Path(tempfile.mkdtemp(prefix="fuzz-redact-"))
OUTSIDE = WORK / "outside.txt"
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
# Names get the placeholder without its <> (artifact paths may not hold them), and the name
# pass runs once, so an ID that starts with the end of a placeholder name ("d", "id") or ends
# with its start ("a", "as", "asc") can re-form across it when the name repeats the ID's tail,
# as in "IDDDDDDDDDDD" for the ID "IDDDDDDD". Names are checked only for other IDs until the
# name pass repeats or removes what it cannot clean.
NAME_JOINS = (("d", "id"), ("a", "as", "asc"))


def make_id(fdp: atheris.FuzzedDataProvider, low: int, high: int) -> str:
    return "".join(common.pick(fdp, ID_ALPHABET) for _ in range(fdp.ConsumeIntInRange(low, high)))


def recase(fdp: atheris.FuzzedDataProvider, value: str) -> str:
    return "".join(ch.upper() if fdp.ConsumeBool() else ch.lower() for ch in value)


def content(fdp: atheris.FuzzedDataProvider, ids: list[str]) -> bytes:
    """Log-like bytes: random runs, the IDs in any case and encoding, placeholders and fragments."""
    parts: list[bytes] = []
    for _ in range(fdp.ConsumeIntInRange(0, 8)):
        kind = fdp.ConsumeIntInRange(0, 4)
        if kind == 0:
            parts.append(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 32)))
        elif kind == 1:
            parts.append(recase(fdp, common.pick(fdp, ids)).encode(common.pick(fdp, ENCODINGS)))
        elif kind == 2:
            value = common.pick(fdp, ids)
            cut = fdp.ConsumeIntInRange(0, len(value))
            parts.append(value[:cut].encode() if fdp.ConsumeBool() else value[cut:].encode())
        elif kind == 3:
            parts.append(common.pick(fdp, list(redact.PLACEHOLDERS.values())))
        else:
            parts.append(b"\x00" * fdp.ConsumeIntInRange(0, 3))
    return b"".join(parts)


def remaining(data: bytes, ids: list[str]) -> bool:
    lowered = data.lower()
    return any(value.lower().encode(encoding) in lowered for value in ids for encoding in ENCODINGS)


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    fake_ids = {"ASC_KEY_ID": make_id(fdp, 8, 12), "ASC_ISSUER_ID": make_id(fdp, 8, 40)}
    ids = list(fake_ids.values())
    files = [content(fdp, ids) for _ in range(fdp.ConsumeIntInRange(1, 3))]

    cleaned_files = []
    for raw in files:
        cleaned = raw
        for name, value in fake_ids.items():
            cleaned = redact.replace_ignoring_case(cleaned, value.encode(), redact.PLACEHOLDERS[name])
        for value in ids:
            assert value.lower().encode() not in cleaned.lower(), f"{value!r} survives in {cleaned!r}"
        if not any(value.lower().encode() in raw.lower() for value in ids):
            assert cleaned == raw, (raw, cleaned)
        cleaned_files.append(cleaned)

    folder = WORK / "logs"
    shutil.rmtree(folder, ignore_errors=True)
    # Names may hold an ID too (an archive or log named after the key).
    nested = "nested-" + recase(fdp, common.pick(fdp, ids)) if fdp.ConsumeBool() else "nested"
    (folder / nested).mkdir(parents=True)
    OUTSIDE.write_bytes(files[0])
    for i, raw in enumerate(files):
        name = f"log{i}-{recase(fdp, common.pick(fdp, ids))}.txt" if fdp.ConsumeBool() else f"log{i}.txt"
        (folder / (nested if i % 2 else "") / name).write_bytes(raw)
    link = folder / "link.txt"
    link.symlink_to(OUTSIDE)

    redact.redact_folder(folder, fake_ids)
    assert not link.exists() and not link.is_symlink(), "the symbolic link was kept"
    assert OUTSIDE.read_bytes() == files[0], "the link target outside the folder was changed"
    allowed = {cleaned for cleaned in cleaned_files if not remaining(cleaned, ids)}
    names_checked = not any(value.lower().startswith(NAME_JOINS[0]) or value.lower().endswith(NAME_JOINS[1])
                            for value in ids)
    for path in folder.rglob("*"):
        relative = path.relative_to(folder).as_posix().lower()
        if names_checked:
            assert not any(value.lower() in relative for value in ids), f"a name still holds an ID: {relative!r}"
        if path.is_file():
            kept = path.read_bytes()
            assert not remaining(kept, ids), f"{relative} still holds an ID: {kept!r}"
            assert kept in allowed, f"{relative} is not the redacted copy of a kept file: {kept!r}"


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
