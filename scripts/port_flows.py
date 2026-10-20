#!/usr/bin/env python3
"""Refresh the ported flows from app checkouts (a maintainer tool; CI never runs it).

    python3 scripts/port_flows.py APPS_DIR

APPS_DIR holds one checkout per app (scowld/, verceltics/, abysm/, pcm-notes/, quit-all/),
each at the commit store_release/flows/port.py names. Every listed script is copied verbatim
to tests/parity/upstream_<package>_<sha9>/ (with MANIFEST.sha256) and ported into
store_release/flows/<package>/. tests/unit/test_flows.py then proves each flow module is
exactly port() of its verbatim copy. Bump a commit in FLOWS first to port a newer app.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from store_release.flows import port  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    apps = Path(argv[0])
    for app, (package, sha, files) in port.FLOWS.items():
        checkout = apps / app
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        if head != sha:
            print(f"error: {checkout} is at {head}, not {sha}", file=sys.stderr)
            return 1
        target = REPO / "store_release" / "flows" / package
        target.mkdir(parents=True, exist_ok=True)
        upstream = REPO / "tests" / "parity" / f"upstream_{package}_{sha[:9]}"
        upstream.mkdir(parents=True, exist_ok=True)
        manifest = []
        for source in list(files) + port.UPSTREAM_TESTS.get(app, []):
            raw = (checkout / source).read_bytes()
            name = port.upstream_name(source)
            (upstream / name).write_bytes(raw)
            manifest.append(f"{hashlib.sha256(raw).hexdigest()}  {name}  {source}")
            if source in files:
                (target / f"{files[source]}.py").write_text(port.port_module(app, source, raw.decode("utf-8")),
                                                             encoding="utf-8")
        (upstream / "MANIFEST.sha256").write_text(
            f"# apoorvdarshan/{app} at {sha}: each file's SHA-256, its name here and its path there.\n"
            + "\n".join(manifest) + "\n", encoding="utf-8")
        print(f"{app}: {len(files)} modules ported from {sha[:9]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
