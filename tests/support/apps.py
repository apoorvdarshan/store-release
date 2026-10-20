"""Configure the ported modules for a synthetic app, and restore them afterwards.

configure_fud_ai_defaults() writes the synthetic Fud AI tree once per process
and configures every module with examples/fud-ai/store-release.toml, so the
ported tests see Fud AI's constants (PACKAGE_NAME, DEFAULT_BUNDLE_ID, ...)
exactly as before. fixture_app() and configured() let a test use another
tree (or the example app) without leaking its configuration.
"""
from __future__ import annotations

import atexit
import contextlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from store_release import catalog, config as config_mod, context, release_notes, screenshots
from store_release.android import play_listing
from store_release.android import release as android_release
from store_release.ios import asc
from store_release.ios import release as ios_release
from store_release.text import listing, whats_new
from support import trees

TOOL_ROOT = trees.TOOL_ROOT
CONFIGURED_MODULES = (release_notes, catalog, listing, whats_new, screenshots, asc, ios_release,
                      android_release, play_listing)


@dataclass
class AppContext:
    name: str
    root: Path
    config: config_mod.Config


_DEFAULT: AppContext | None = None


def snapshot() -> dict:
    """Every upper-case global of the configured modules (the app constants)."""
    return {module: {name: value for name, value in vars(module).items() if name.isupper()}
            for module in CONFIGURED_MODULES}


def restore(saved: dict) -> None:
    for module, values in saved.items():
        for name, value in values.items():
            setattr(module, name, value)


def write_tree(name: str, root: Path, **kwargs) -> Path:
    if name == "fud-ai":
        return trees.write_fud_ai_tree(root, **kwargs)
    if name == "example":
        return trees.write_example_tree(root, **kwargs)
    raise ValueError(name)


def fixture_app(name: str, tmp: Path, **kwargs) -> AppContext:
    """Write the named synthetic app under tmp and load its config (not configured yet)."""
    root = write_tree(name, Path(tmp), **kwargs)
    return AppContext(name, root, config_mod.load_for_app(root))


@contextlib.contextmanager
def configured(app: AppContext) -> Iterator[AppContext]:
    saved = snapshot()
    context.configure(app.config, app.root)
    try:
        yield app
    finally:
        restore(saved)


def configure_fud_ai_defaults() -> AppContext:
    """The synthetic Fud AI tree, once per process, with every module configured for it."""
    global _DEFAULT
    if _DEFAULT is None:
        tmp = Path(tempfile.mkdtemp(prefix="store-release-fud-ai-"))
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)
        _DEFAULT = fixture_app("fud-ai", tmp / "fud-ai")
    context.configure(_DEFAULT.config, _DEFAULT.root)
    return _DEFAULT


def default_app() -> AppContext:
    return configure_fud_ai_defaults() if _DEFAULT is None else _DEFAULT
