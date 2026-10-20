"""Synthetic app trees for the ported flows' tests (no app content beyond each app's MIT-licensed
project file and product catalog, in tests/flows/fixtures/<package>/).

build_tree(app, root) writes:
  store/store-release.toml        examples/<app>/store-release.toml
  <project>/project.pbxproj       the app's project file at its flow commit
  store/catalog/products.json     the app's product catalog at its flow commit (apps with one)
  <android gradle file>           the app's build.gradle.kts at its flow commit (Verceltics)
  <Info.plist>                    the app target's Info.plist where its project names one
  RELEASE_NOTES.md                one synthetic section
  store/creative/ios/<slot>/      README.md only (no image)
  store/metadata/ios/en-US/       synthetic listing text
and copies the app's own test files into scripts/store/, where their ROOT is this tree.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from store_release import config as config_mod
from store_release.flows import port

TOOL_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = TOOL_ROOT / "tests" / "flows" / "fixtures"
TEST_FILES = {
    "scowld": ["test_ios_release.py"],
    "verceltics": ["test_ios_release.py", "test_android_release.py"],
    "abysm": ["test_ios_release.py"],
    "pcm-notes": ["test_ios_release.py"],
    "quit-all": ["test_ios_release.py"],
}
INFO_PLISTS = {"abysm": "Abysm/Info.plist", "verceltics": "ios/verceltics-Info.plist"}
HELPERS = {"print_gates.py": "scripts/store/print_gates.py", "redact_logs.py": "scripts/store/redact_logs.py",
           "release_notes.py": "scripts/release_notes.py", "ci_redact_ids.py": "scripts/ci/redact_ids.py",
           "verify_ios_bundles.sh": "scripts/store/verify_ios_bundles.sh"}
LISTING = {
    "name.txt": "Example App",
    "subtitle.txt": "An example",
    "description.txt": "A synthetic description for the store-release tests.",
    "keywords.txt": "example,test",
    "support_url.txt": "https://example.com/support",
    "privacy_url.txt": "https://example.com/privacy",
}


def package(app: str) -> str:
    return port.FLOWS[app][0]


def upstream_dir(app: str) -> Path:
    name, sha, _ = port.FLOWS[app]
    return TOOL_ROOT / "tests" / "parity" / f"upstream_{name}_{sha[:9]}"


def build_tree(app: str, root: Path) -> Path:
    root = Path(root)
    (root / "store").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TOOL_ROOT / "examples" / app / "store-release.toml", root / "store" / "store-release.toml")
    cfg = config_mod.load_for_app(root)
    fixtures = FIXTURES / package(app)
    pbxproj = root / cfg.ios.pbxproj
    pbxproj.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixtures / "project.pbxproj", pbxproj)
    # The app target's Info.plist where the project names one (INFOPLIST_FILE).
    for app_name, relative in INFO_PLISTS.items():
        if app_name == app:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixtures / "Info.plist", path)
    if cfg.android is not None:
        gradle = root / cfg.android.gradle_file
        gradle.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixtures / "build.gradle.kts", gradle)
    if cfg.catalog is not None:
        catalog = root / cfg.catalog.path
        catalog.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixtures / "products.json", catalog)
    (root / "RELEASE_NOTES.md").write_text(f"# Release notes\n\n## {cfg.ios.tag_prefix}9.9.9\n\n- Synthetic notes.\n",
                                           encoding="utf-8")
    creative = root / (cfg.ios.creative_dir or "store/creative/ios")
    for slot in ("header", "search-results"):
        (creative / slot).mkdir(parents=True, exist_ok=True)
        (creative / slot / "README.md").write_text("No image yet.\n", encoding="utf-8")
    locale = root / (cfg.ios.listing.dir if cfg.ios.listing else "store/metadata/ios") / "en-US"
    locale.mkdir(parents=True, exist_ok=True)
    for name, text in LISTING.items():
        (locale / name).write_text(text + "\n", encoding="utf-8")
    scripts = root / "scripts" / "store"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in TEST_FILES[app]:
        shutil.copyfile(upstream_dir(app) / name, scripts / name)
    # The app's own helpers its tests import. store-release replaces them with its generic
    # print-switches, redact and release-notes, so their tests are listed as not ported.
    for name, target in HELPERS.items():
        source = upstream_dir(app) / name
        if source.is_file():
            path = root / target
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
    if app == "abysm":
        # ABYSM's tests read its simulator test runner at import; the runner stays in the app
        # (IosTestScriptTests are app-side, tests/flows/abysm.inventory).
        (root / "scripts" / "test-ios.sh").write_text("#!/bin/bash\n# stays in the app\n", encoding="utf-8")
        # Its workflow-wiring tests read the app's own release workflow at import; store-release's
        # workflow tests cover the reusable workflows instead (abysm.inventory).
        (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (root / ".github" / "workflows" / "ios-release.yml").write_text("name: placeholder\n", encoding="utf-8")
    return root
