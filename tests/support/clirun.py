"""Run store-release's CLI against a synthetic app: as a subprocess (run.py), or in-process (cli.main)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release import cli
from support import apps as support_apps

TOOL_ROOT = support_apps.TOOL_ROOT
RUN_PY = TOOL_ROOT / "run.py"


def clean_env(**extra: str) -> dict[str, str]:
    """The test process's environment without switches, store flags or GitHub run context."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("IOS_", "ANDROID_", "STORE_", "GITHUB_", "UPLOAD_", "SUBMIT_", "PUBLIC_", "HAS_"))
           and k not in ("CREATE_IOS_VERSION", "PLAY_SERVICE_ACCOUNT_JSON", "TOOL_SHA", "TOOL_REPOSITORY")}
    env.update(extra)
    return env


def run(app_root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-u", str(RUN_PY), "--app-root", str(app_root), *args],
                          capture_output=True, text=True, env=env if env is not None else clean_env())


def outputs(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    return values


def in_process(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """cli.main() in this process (for fakes patched in), module configuration restored afterwards."""
    out, err = io.StringIO(), io.StringIO()
    saved = support_apps.snapshot()
    try:
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if exc.code and not isinstance(exc.code, int):
                    err.write(str(exc.code))
    finally:
        support_apps.restore(saved)
    return code, out.getvalue(), err.getvalue()


FAKE_GH = '''#!/usr/bin/env python3
"""A fake gh: logs argv, answers release list/view from GH_FAKE_STATE."""
import json, os, sys
state = json.loads(open(os.environ["GH_FAKE_STATE"]).read())
with open(os.environ["GH_FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["release", "list"]:
    print(json.dumps([{"tagName": t, "name": n} for t, n in state.get("releases", [])]))
elif args[:2] == ["release", "view"]:
    sys.exit(0 if args[2] in state.get("existing", []) else 1)
'''


def fake_gh(folder: Path, *, releases: list[tuple[str, str]] = (), existing: list[str] = ()) -> dict[str, str]:
    """A bin folder with a fake gh; returns the env entries that put it first on PATH."""
    folder.mkdir(parents=True, exist_ok=True)
    gh = folder / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    state = folder / "state.json"
    state.write_text(json.dumps({"releases": list(releases), "existing": list(existing)}))
    log = folder / "gh.log"
    log.write_text("")
    return {"PATH": f"{folder}{os.pathsep}{os.environ.get('PATH', '')}", "GH_FAKE_STATE": str(state),
            "GH_FAKE_LOG": str(log)}


def gh_calls(env: dict[str, str]) -> list[list[str]]:
    return [json.loads(line) for line in Path(env["GH_FAKE_LOG"]).read_text().splitlines() if line]
