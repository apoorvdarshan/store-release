"""A fake GitHub API (urlopen) for store-release's release tags: GET git/ref/tags/<tag> and git/tags/<sha>."""
from __future__ import annotations

import hashlib
import io
import json
import urllib.error


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TagApi:
    """tags: {"v0.1.0": commit sha}. annotated=True serves each through an annotated tag object.
    error: raised for every request instead (an HTTPError or URLError)."""

    def __init__(self, tags: dict[str, str] | None = None, *, annotated: bool = False,
                 error: Exception | None = None, repository: str = "apoorvdarshan/store-release") -> None:
        self.tags = dict(tags or {})
        self.annotated = annotated
        self.error = error
        self.prefix = f"https://api.github.com/repos/{repository}/"
        self.requests = []

    def tag_object(self, name: str) -> str:
        return hashlib.sha1(f"annotated tag {name}".encode()).hexdigest()

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        url = request.full_url
        if request.get_method() != "GET" or not url.startswith(self.prefix):
            raise AssertionError(f"unexpected request {request.get_method()} {url}")
        path = url[len(self.prefix):]
        if path.startswith("git/ref/tags/"):
            name = path[len("git/ref/tags/"):]
            if name not in self.tags:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
            target = ({"type": "tag", "sha": self.tag_object(name)} if self.annotated
                      else {"type": "commit", "sha": self.tags[name]})
            return Response(json.dumps({"ref": f"refs/tags/{name}", "object": target}).encode())
        if path.startswith("git/tags/"):
            sha = path[len("git/tags/"):]
            for name, commit in self.tags.items():
                if self.tag_object(name) == sha:
                    return Response(json.dumps({"tag": name, "object": {"type": "commit", "sha": commit}}).encode())
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        raise AssertionError(f"unexpected request {url}")
