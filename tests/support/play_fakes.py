"""A fake androidpublisher service: edits, bundles, APKs, tracks, listings and images, in memory."""
from __future__ import annotations

from typing import Any


class _Request:
    def __init__(self, play: "FakePlay", result: Any) -> None:
        self.play = play
        self.result = result

    def execute(self) -> Any:
        return self.result() if callable(self.result) else self.result


class _Resource:
    def __init__(self, play: "FakePlay", name: str) -> None:
        self.play = play
        self.name = name

    def __getattr__(self, method: str):
        if method.startswith("__"):
            raise AttributeError(method)

        def call(**kwargs):
            self.play.calls.append((f"{self.name}.{method}", kwargs))
            return _Request(self.play, lambda: self.play.answer(self.name, method, kwargs))

        return call


class _Edits:
    def __init__(self, play: "FakePlay") -> None:
        self.play = play

    def insert(self, **kwargs):
        self.play.calls.append(("edits.insert", kwargs))
        self.play.opened += 1
        return _Request(self.play, {"id": f"E{self.play.opened}"})

    def delete(self, **kwargs):
        self.play.calls.append(("edits.delete", kwargs))
        return _Request(self.play, {})

    def commit(self, **kwargs):
        self.play.calls.append(("edits.commit", kwargs))
        return _Request(self.play, {})

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda: _Resource(self.play, name)


class FakePlay:
    """Google Play's state for one app: known versionCodes, tracks, the listing and phone screenshots."""

    def __init__(self, *, bundles: list[int] = (), apks: list[int] = (), tracks: dict[str, list[tuple[int, str]]] | None = None,
                 listing: dict[str, str] | None = None, images: list[str] = ()) -> None:
        self.bundles = list(bundles)
        self.apks = list(apks)
        self.tracks = {name: list(releases) for name, releases in (tracks or {}).items()}
        self.listing = dict(listing or {})
        self.images = list(images)
        self.calls: list[tuple[str, dict]] = []
        self.opened = 0

    def edits(self) -> _Edits:
        return _Edits(self)

    def track(self, name: str) -> dict:
        return {"track": name, "releases": [{"versionCodes": [str(code)], "status": status}
                                            for code, status in self.tracks.get(name, [])]}

    def answer(self, resource: str, method: str, kwargs: dict) -> Any:
        if resource == "bundles" and method == "list":
            return {"bundles": [{"versionCode": code} for code in self.bundles]}
        if resource == "apks" and method == "list":
            return {"apks": [{"versionCode": code} for code in self.apks]}
        if resource == "tracks" and method == "list":
            return {"tracks": [self.track(name) for name in self.tracks]}
        if resource == "tracks" and method == "get":
            return self.track(kwargs["track"])
        if resource == "listings" and method == "get":
            return dict(self.listing, language=kwargs.get("language"))
        if resource == "images" and method == "list":
            return {"images": [{"id": f"IMG{i}", "sha256": digest} for i, digest in enumerate(self.images)]}
        if resource == "details" and method == "get":
            return {"defaultLanguage": "en-US"}
        return {}

    def writes(self) -> list[str]:
        reads = {"edits.insert", "edits.delete", "bundles.list", "apks.list", "tracks.list", "tracks.get",
                 "listings.get", "listings.list", "images.list", "details.get"}
        return [name for name, _ in self.calls if name not in reads]
