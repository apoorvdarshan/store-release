#!/usr/bin/env python3
"""Render examples/<app>/callers/ and examples/templates/ (a maintainer tool).

    python3 scripts/render_examples.py [--check]

Each app gets the caller of every platform its config has, plus store-checks.yml, pinned to
the placeholder commit 0000000000000000000000000000000000000000 with this package's version.
Each template (examples/templates/<name>/) is exactly what `run.py init` writes for its
answers (store_release.scaffold.TEMPLATES): the config, the callers and dependabot.yml.
--check fails instead of writing when a committed file differs (tests/workflows run it).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from store_release import __version__, callers, config, scaffold  # noqa: E402

SHA = "0" * 40
APPS = {"fud-ai": "apoorvdarshan/fud-ai", "scowld": "apoorvdarshan/scowld", "verceltics": "apoorvdarshan/verceltics",
        "abysm": "apoorvdarshan/abysm", "pcm-notes": "apoorvdarshan/pcm-notes", "quit-all": "apoorvdarshan/quit-all"}


def rendered(app: str) -> dict[str, str]:
    cfg = config.load(REPO / "examples" / app / "store-release.toml")
    out = {}
    for platform, name in callers.CALLER_FILES.items():
        if platform == "ios" and cfg.ios is None or platform == "android" and cfg.android is None:
            continue
        out[name] = callers.render(cfg, platform, sha=SHA, version=__version__,
                                   app_repository=APPS[app])
    return out


def expected() -> dict[Path, str]:
    files = {}
    for app in APPS:
        for name, text in rendered(app).items():
            files[REPO / "examples" / app / "callers" / name] = text
    for name in scaffold.TEMPLATES:
        for relative, text in scaffold.template_files(name).items():
            files[REPO / "examples" / "templates" / name / relative] = text
    return files


def main(argv: list[str]) -> int:
    check = argv == ["--check"]
    stale = []
    for path, text in expected().items():
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(str(path.relative_to(REPO)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if stale:
        print("error: re-render these with scripts/render_examples.py: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
