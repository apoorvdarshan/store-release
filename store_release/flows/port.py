"""How each flow module was ported from its app (the only edits; tests/unit/test_flows.py proves it).

port() turns an app's script into its flow module:
  - ROOT = Path(__file__).resolve().parents[N]  ->  ROOT = UNCONFIGURED_ROOT (configure() sets it)
  - sys.path.insert(...) lines are removed; sibling imports become relative (from . import x)
  - the app's identity literals (bundle id, App Store Connect app id, tag prefix, locale, Play
    package, upload certificate) become UNCONFIGURED_TEXT; configure() sets them from the config
  - an import the edits leave unused (sys, Path) is removed
Run scripts/port_flows.py with the app checkouts to refresh a flow from a newer app commit.
"""
from __future__ import annotations

import re

FLOWS = {
    # app repo, python package, commit, {source path: flow module name}
    "scowld": ("scowld", "5283a488f84dfda7626474a6a99ce034c2de4db7", {
        "scripts/store/asc_release.py": "asc_release",
        "scripts/store/ios_release.py": "ios_release",
        "scripts/store/asc_export_listing.py": "asc_export_listing",
    }),
    "verceltics": ("verceltics", "30378f64921232eed032561587d8461de554df06", {
        "scripts/store/asc_release.py": "asc_release",
        "scripts/store/ios_release.py": "ios_release",
        "scripts/store/ios_metadata.py": "ios_metadata",
        "scripts/store/export_ios_listing.py": "export_ios_listing",
        "scripts/store/android_release.py": "android_release",
        "scripts/store/play_listing.py": "play_listing",
        "scripts/store/validate_catalog.py": "validate_catalog",
        "scripts/release_notes.py": "release_notes",
    }),
    "abysm": ("abysm", "a12a6d24ba3f47712844aebe370214f25d4bea50", {
        "scripts/store/asc_release.py": "asc_release",
        "scripts/store/ios_release.py": "ios_release",
        "scripts/store/export_listing.py": "export_listing",
        "scripts/store/validate_catalog.py": "validate_catalog",
    }),
    "pcm-notes": ("pcm_notes", "517b61a78f6603e7ed5164e53d811979a5caa64d", {
        "scripts/store/asc_release.py": "asc_release",
        "scripts/store/ios_release.py": "ios_release",
        "scripts/store/export_listing.py": "export_listing",
        "scripts/store/validate_catalog.py": "validate_catalog",
    }),
    "quit-all": ("quit_all", "34b81989a8df2aa7c7cd05ee9aaf4b41c114b87e", {
        "scripts/store/asc_release.py": "asc_release",
        "scripts/store/ios_release.py": "ios_release",
    }),
}

# The upstream test files kept verbatim beside the scripts (the ported tests come from them).
UPSTREAM_TESTS = {
    "scowld": ["scripts/store/test_ios_release.py"],
    "verceltics": ["scripts/store/test_ios_release.py", "scripts/store/test_android_release.py",
                   "scripts/store/test_store_assets.py", "scripts/ci/test_redact_ids.py",
                   "scripts/ci/redact_ids.py", "scripts/store/check_store_tooling.py",
                   "scripts/store/print_gates.py"],
    "abysm": ["scripts/store/test_ios_release.py", "scripts/store/redact_logs.py", "scripts/store/print_gates.py",
              "scripts/release_notes.py", "fuzz/redact_logs_fuzz.py", "fuzz/release_gates_fuzz.py",
              "fuzz/release_notes_fuzz.py"],
    "pcm-notes": ["scripts/store/test_ios_release.py", "scripts/store/redact_logs.py",
                  "scripts/store/print_gates.py", "scripts/release_notes.py", "fuzz/fuzz_build_selection.py",
                  "fuzz/fuzz_release_gates.py", "fuzz/fuzz_release_notes.py"],
    "quit-all": ["scripts/store/test_ios_release.py", "scripts/store/redact_logs.py",
                 "scripts/store/print_gates.py", "scripts/release_notes.py", "scripts/store/verify_ios_bundles.sh"],
}
UPSTREAM_TESTS["scowld"] += ["scripts/store/redact_logs.py", "scripts/store/print_gates.py", "scripts/release_notes.py"]

ROOT_LINE = re.compile(r"^ROOT = Path\(__file__\)\.resolve\(\)\.parents\[[0-9]\]\s*$", re.M)
SYS_PATH = re.compile(r"^\s*sys\.path\.insert\(0, str\((Path\(__file__\)\.resolve\(\)\.parent|ROOT / \"scripts\"|HERE)\)\)\s*\n",
                      re.M)


# Identity literals that come from store-release.toml (configure() sets them); the
# vendored source keeps the name with a placeholder, so nothing app-specific is hard-coded.
IDENTITY = {
    ("scowld", "asc_release"): ("DEFAULT_BUNDLE_ID", "DEFAULT_APP_ID", "CREATIVE_LOCALE"),
    ("scowld", "ios_release"): ("BUNDLE_ID", "TAG_PREFIX"),
    ("verceltics", "asc_release"): ("DEFAULT_BUNDLE_ID", "DEFAULT_APP_ID"),
    ("verceltics", "ios_metadata"): ("PRIMARY_LOCALE",),
    ("verceltics", "ios_release"): ("BUNDLE_ID", "TAG_PREFIX"),
    ("verceltics", "android_release"): ("PACKAGE_NAME", "TAG_PREFIX", "EXPECTED_UPLOAD_CERT_SHA256"),
    ("verceltics", "play_listing"): ("PACKAGE_NAME", "PRIMARY_LOCALE"),
    ("verceltics", "validate_catalog"): ("PACKAGE",),
    ("abysm", "asc_release"): ("DEFAULT_BUNDLE_ID", "APP_STORE_APP_ID", "PRIMARY_LOCALE"),
    ("abysm", "ios_release"): ("BUNDLE_ID", "APP_STORE_APP_ID", "TAG_PREFIX"),
    ("abysm", "validate_catalog"): ("BUNDLE_ID",),
    ("pcm_notes", "asc_release"): ("DEFAULT_BUNDLE_ID", "APP_STORE_APP_ID", "PRIMARY_LOCALE"),
    ("pcm_notes", "ios_release"): ("BUNDLE_ID", "TAG_PREFIX"),
    ("pcm_notes", "validate_catalog"): ("BUNDLE_ID",),
    ("quit_all", "asc_release"): ("DEFAULT_BUNDLE_ID", "DEFAULT_APP_ID", "CREATIVE_LOCALE"),
    ("quit_all", "ios_release"): ("BUNDLE_ID", "ASC_APP_ID", "TAG_PREFIX"),
}


def placeholders(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        text, count = re.subn(rf'^{name} = "[^"\n]*"', f"{name} = UNCONFIGURED_TEXT  # store-release: from the config",
                              text, flags=re.M)
        assert count == 1, f"{name}: expected one literal assignment"
    if names:
        text = text.replace("from store_release.flows import UNCONFIGURED_ROOT  # store-release",
                            "from store_release.flows import UNCONFIGURED_ROOT, UNCONFIGURED_TEXT  # store-release")
    return text


def drop_unused(text: str) -> str:
    """The sys.path and ROOT edits can leave `import sys` or `from pathlib import Path` unused."""
    for line, word in (("import sys\n", "sys."), ("from pathlib import Path\n", "Path")):
        if line in text:
            rest = text.replace(line, "", 1)
            if not re.search(rf"\b{re.escape(word)}", rest):
                text = rest
    return text


def port(text: str, siblings: set[str]) -> str:
    text, count = ROOT_LINE.subn("ROOT = UNCONFIGURED_ROOT  # store-release: set by flow.configure()", text)
    assert count <= 1, "expected at most one ROOT line"
    text = SYS_PATH.sub("", text)
    for name in sorted(siblings):
        # import X [as Y]  ->  from . import X [as Y]
        text = re.sub(rf"^(\s*)import {name}(\s+as\s+\w+)?(\s*#.*)?$", rf"\1from . import {name}\2\3", text, flags=re.M)
        # from X import ...  ->  from .X import ...
        text = re.sub(rf"^(\s*)from {name} import ", rf"\1from .{name} import ", text, flags=re.M)
    # The placeholder root lives in the flows package.
    marker = "from __future__ import annotations\n"
    if not count:
        return text
    if marker in text:
        head, tail = text.split(marker, 1)
        text = head + marker + "\nfrom store_release.flows import UNCONFIGURED_ROOT  # store-release\n" + tail
    else:
        raise AssertionError("module lacks from __future__ import annotations")
    return text


def upstream_name(source: str) -> str:
    """A script's file name in its tests/parity/upstream_<package>_<sha9>/ copy."""
    name = source.replace("scripts/store/", "").replace("scripts/ci/", "ci_").replace("scripts/", "")
    return name.replace("fuzz/", "fuzz_")


def port_module(app: str, source: str, text: str) -> str:
    """The flow module store-release keeps for one of an app's scripts."""
    package, _, files = FLOWS[app]
    module = files[source]
    return drop_unused(placeholders(port(text, set(files.values())), IDENTITY.get((package, module), ())))
