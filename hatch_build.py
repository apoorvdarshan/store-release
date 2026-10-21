"""Hatchling metadata hook (pyproject.toml): the PyPI description.

README.md links the docs relatively, which works on GitHub but not on PyPI. The package's
description is README.md with every relative link pointed at this version's tag on GitHub,
so the PyPI page links the docs of the version it describes. Links inside fenced code
blocks, absolute links and in-page anchors are left alone. Not part of the package.
"""
from __future__ import annotations

import re
from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface

REPOSITORY = "https://github.com/apoorvdarshan/store-release"
LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
FENCE = re.compile(r"^\s*(```|~~~)")


def absolute_links(text: str, version: str, repository: str = REPOSITORY) -> str:
    """text with each relative Markdown link and image pointed at tag v<version> of repository."""

    def replace(match: re.Match[str]) -> str:
        bang, label, target = match.groups()
        if SCHEME.match(target) or target.startswith(("#", "/")):
            return match.group(0)
        path, hash_sign, anchor = target.partition("#")
        path = path.removeprefix("./")
        kind = "raw" if bang else ("tree" if path.endswith("/") else "blob")
        return f"{bang}[{label}]({repository}/{kind}/v{version}/{path}{hash_sign}{anchor})"

    out: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        opened = FENCE.match(line)
        if fence:
            if opened and opened.group(1) == fence:
                fence = ""
            out.append(line)
        elif opened:
            fence = opened.group(1)
            out.append(line)
        else:
            out.append(LINK.sub(replace, line))
    return "".join(out)


class PypiReadmeHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        text = (Path(self.root) / "README.md").read_text(encoding="utf-8")
        metadata["readme"] = {"content-type": "text/markdown", "text": absolute_links(text, metadata["version"])}
