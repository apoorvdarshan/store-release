"""An App Store Connect fake for store plans: the CreativeFake's state, reads only.

Plans must never send a write, so every non-GET method of this transport fails
the test; `methods` records what reached it.
"""
from __future__ import annotations

import urllib.parse

from support.asc_fakes import APP, VERSION, CreativeFake


class PlanAsc(CreativeFake):
    """CreativeFake as a read-only transport, with a configurable version list and uploaded builds."""

    def __init__(self, *, versions: list[tuple[str, str]] | None = None, builds: list[str] = ("40",), **kwargs) -> None:
        super().__init__(**kwargs)
        # (versionString, appStoreState); the default is this release's version in self.state.
        self.versions = versions
        self.builds = list(builds)
        self.methods: list[str] = []

    def get(self, path: str) -> dict:
        self.methods.append("GET")
        parsed = urllib.parse.urlparse(path)
        if parsed.path == f"/apps/{APP}/appStoreVersions" and self.versions is not None:
            self.calls.append(("GET", path))
            return {"data": [{"id": "V1" if number == VERSION else f"V-{number}",
                              "attributes": {"versionString": number, "appStoreState": state, "platform": "IOS"}}
                             for number, state in self.versions]}
        if parsed.path == "/builds":
            self.calls.append(("GET", path))
            assert urllib.parse.parse_qs(parsed.query).get("filter[app]") == [APP], path
            return {"data": [{"type": "builds", "id": f"B{n}", "attributes": {"version": n}} for n in self.builds]}
        if parsed.path.startswith("/appStoreVersions/V-"):
            # Every version shares the one fake localization (as a new version copies the last one).
            rest = parsed.path.split("/", 3)[3]
            return super().get(f"/appStoreVersions/V1/{rest}" + (f"?{parsed.query}" if parsed.query else ""))
        return super().get(path)

    def post(self, path, body):  # noqa: ARG002
        self.methods.append("POST")
        raise AssertionError(f"a store plan sent POST {path}")

    def patch(self, path, body):  # noqa: ARG002
        self.methods.append("PATCH")
        raise AssertionError(f"a store plan sent PATCH {path}")

    def request(self, method, path, body=None):  # noqa: ARG002
        self.methods.append(method)
        raise AssertionError(f"a store plan sent {method} {path}")

    def upload_bytes(self, url, data, headers, *, method="PUT"):  # noqa: ARG002
        self.methods.append("UPLOAD")
        raise AssertionError(f"a store plan uploaded to {url}")
