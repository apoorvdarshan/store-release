"""Yanked store-release versions: docs/yanked.md on the tooling repository's main branch.

`yanked check --warn-only` (release prepare/gates) turns a listed version or a
fetch error into a warning annotation, so a release never depends on mutable
remote state. Without --warn-only (an app's pull request checks) a listed
version fails, and so does a fetch error.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping

from store_release import __version__

LISTED = re.compile(r"^- v(\d+\.\d+\.\d+)\b", re.MULTILINE)
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class YankedError(Exception):
    """This store-release version is yanked, or the list could not be read."""


def listed_versions(text: str) -> set[str]:
    return set(LISTED.findall(text))


def fetch(repository: str, token: str, *, api: str = "https://api.github.com",
          opener: Callable = urllib.request.urlopen) -> str:
    if not REPOSITORY.fullmatch(repository or ""):
        raise YankedError(f"TOOL_REPOSITORY {repository!r} is not owner/name")
    url = f"{api.rstrip('/')}/repos/{repository}/contents/docs/yanked.md?" + urllib.parse.urlencode({"ref": "main"})
    headers = {"Accept": "application/vnd.github.raw", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with opener(request, timeout=30) as response:
        return response.read().decode("utf-8")


def check(*, warn_only: bool, environ: Mapping[str, str] | None = None, opener: Callable = urllib.request.urlopen,
          version: str = __version__) -> str:
    env = os.environ if environ is None else environ
    try:
        text = fetch(env.get("TOOL_REPOSITORY", ""), env.get("GH_TOKEN", ""),
                     api=env.get("GITHUB_API_URL", "https://api.github.com"), opener=opener)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, YankedError) as exc:
        message = f"could not read the yanked list; re-run ({exc})"
        if warn_only:
            print(f"::warning::{message}")
            return "warning"
        raise YankedError(message) from None
    if version in listed_versions(text):
        message = (f"store-release v{version} is yanked (docs/yanked.md); "
                   "update the pin to the fixed release")
        if warn_only:
            print(f"::warning::{message}")
            return "warning"
        raise YankedError(message)
    print(f"store-release v{version} is not yanked")
    return "ok"
