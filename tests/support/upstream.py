"""Import fud-ai's verbatim scripts (tests/parity/upstream_fud_ai_a99591908) in isolation.

The scripts are copied into <tree>/scripts/store/ and <tree>/scripts/, so their
ROOT (Path(__file__).resolve().parents[...]) is the synthetic tree, then
imported with sys.path and sys.modules snapshotted and restored, so their bare
module names (ios_release, asc_release, ...) never leak into other tests.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from support import trees

UPSTREAM_DIR = trees.UPSTREAM
STORE_SCRIPTS = ("ios_release", "asc_release", "android_release", "play_listing", "prepare_listing_text",
                 "prepare_whats_new", "stage_screenshots", "print_gates", "redact_credential_ids",
                 "validate_catalog", "sync_revenuecat_catalog")


class Upstream:
    """The upstream modules for one synthetic tree, as attributes (up.asc_release, ...)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        store = self.root / "scripts" / "store"
        store.mkdir(parents=True, exist_ok=True)
        for name in STORE_SCRIPTS:
            shutil.copyfile(UPSTREAM_DIR / f"{name}.py", store / f"{name}.py")
        shutil.copyfile(UPSTREAM_DIR / "release_notes.py", self.root / "scripts" / "release_notes.py")
        saved_path = list(sys.path)
        saved_modules = set(sys.modules)
        names = (*STORE_SCRIPTS, "release_notes")
        clashes = {name: sys.modules.pop(name) for name in names if name in sys.modules}
        try:
            sys.path.insert(0, str(self.root / "scripts"))
            sys.path.insert(0, str(store))
            self.modules: dict[str, ModuleType] = {name: importlib.import_module(name) for name in names}
        finally:
            sys.path[:] = saved_path
            for name in set(sys.modules) - saved_modules:
                sys.modules.pop(name, None)
            sys.modules.update(clashes)
        for name, module in self.modules.items():
            setattr(self, name, module)

    @contextlib.contextmanager
    def importable(self):
        """Let the upstream modules' lazy imports (asc_client, play_publisher) find their siblings."""
        saved = {name: sys.modules.get(name) for name in self.modules}
        sys.modules.update(self.modules)
        try:
            yield
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


def outcome(fn: Callable[[], Any]) -> tuple[str, Any, str, str]:
    """(kind, value, stdout, stderr): kind is "ok", "exit" or the exception's class name."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            value = fn()
        except SystemExit as exc:
            code = exc.code
            if not isinstance(code, int):
                err.write(f"{code}\n" if code else "")
                code = 1 if code else 0
            return "exit", code, out.getvalue(), err.getvalue()
        except Exception as exc:  # noqa: BLE001 - compared, not handled
            return type(exc).__name__, str(exc), out.getvalue(), err.getvalue()
    return "ok", value, out.getvalue(), err.getvalue()


def strip_usage(text: str) -> str:
    """argparse's usage lines name the program and its options; the error line is what counts."""
    lines = text.splitlines()
    kept = []
    skipping = False
    for line in lines:
        if line.startswith("usage:"):
            skipping = True
            continue
        if skipping and line.startswith(" "):
            continue
        skipping = False
        kept.append(line)
    return "\n".join(kept)


def tree_files(folder: Path) -> dict[str, bytes]:
    if not folder.exists():
        return {}
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file()}


class ParityCase(unittest.TestCase):
    """A fresh synthetic Fud AI tree per test, the upstream scripts for it, and the package configured for it."""

    screenshots = False

    def setUp(self) -> None:
        from support import apps as support_apps  # noqa: PLC0415 - apps imports the package modules

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.app = support_apps.fixture_app("fud-ai", self.tmp / "app", screenshots=self.screenshots)
        self.up = Upstream(self.app.root)
        configured = support_apps.configured(self.app)
        configured.__enter__()
        self.addCleanup(configured.__exit__, None, None, None)
