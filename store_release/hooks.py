"""Run app hook scripts (catalog.checks, ios.build.pre_archive) with a scrubbed environment.

Hooks are app code with the same trust as an Xcode build phase. They still
never see credentials: every variable whose name looks like one is removed
before the script starts, and the step that runs them maps no secret.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

SCRUB_CONTAINS = ("TOKEN", "SECRET", "PASSWORD", "PASSPHRASE", "CREDENTIAL", "_KEY", "KEY_", "P12", "P8")
SCRUB_PREFIXES = ("ASC_", "APP_STORE_CONNECT_", "ANDROID_KEY", "PLAY_SERVICE_ACCOUNT", "GH_",
                  "ACTIONS_ID_TOKEN_REQUEST_", "ACTIONS_RUNTIME_")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+\.(sh|py)")


class HookError(Exception):
    """A hook script is missing, unsafe, or exited non-zero."""


def scrubbed(environ: Mapping[str, str]) -> dict[str, str]:
    """environ without anything that could hold a credential."""
    clean = {}
    for name, value in environ.items():
        upper = name.upper()
        if any(part in upper for part in SCRUB_CONTAINS) or upper.startswith(SCRUB_PREFIXES):
            continue
        clean[name] = value
    return clean


def command(script: str) -> list[str]:
    if script.endswith(".sh"):
        return ["bash", script]
    if script.endswith(".py"):
        return ["python3", "-u", script]
    raise HookError(f"hook {script} must be a .sh or .py script")


def run(app_root: Path, scripts: Sequence[str], *, env: Mapping[str, str] | None = None,
        env_extra: Mapping[str, str] | None = None) -> None:
    """Run each repository-relative script with cwd = app_root; a non-zero exit stops the run."""
    root = app_root.resolve()
    child_env = scrubbed(os.environ if env is None else env)
    child_env.update(env_extra or {})
    for script in scripts:
        if not SAFE_PATH.fullmatch(script) or ".." in script.split("/"):
            raise HookError(f"hook {script!r} is not a safe repository path")
        path = (root / script).resolve()
        if root not in path.parents or not path.is_file():
            raise HookError(f"hook {script} is not a file in the app repository")
        print(f"hook: {script}", flush=True)
        result = subprocess.run(command(script), cwd=root, env=child_env, check=False)
        if result.returncode != 0:
            raise HookError(f"hook {script} exited {result.returncode}")
