"""Synthetic app repositories for tests: Fud AI's shape and the example app's.

Everything is generated at test time (no binary is committed and no app listing
copy is copied in). The Fud AI tree mirrors the real repository's layout, so
the ported tests that read "the repository's" files read these instead.
"""
from __future__ import annotations

import functools
import json
import plistlib
import shutil
import tomllib
from pathlib import Path

from support import pngs

TOOL_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = TOOL_ROOT / "examples"
UPSTREAM = TOOL_ROOT / "tests" / "parity" / "upstream_fud_ai_a99591908"

FUD_AI_TARGETS = (
    "calorietracker",
    "calorietrackerTests",
    "calorietrackerUITests",
    "FudAIWidgetsExtension",
    "calorietrackerShare",
    "FudAIWatchApp",
    "FudAIWatchWidgetsExtension",
)
PREFIX = "com.apoorvdarshan.calorietracker"


def pbxproj(targets: dict[str, dict[str, str]], *, project_name: str, marketing: str, build: str) -> str:
    """A minimal project.pbxproj: each target with Debug and Release, plus project-level configurations."""
    ids = iter(f"{n:024X}" for n in range(1, 10000))
    objects: list[str] = []

    def config_list(owner: str, configs: dict[str, dict[str, str]]) -> str:
        list_id = next(ids)
        refs = []
        for name, settings in configs.items():
            config_id = next(ids)
            refs.append(f"\t\t\t\t{config_id} /* {name} */,\n")
            lines = "".join(f"\t\t\t\t{key} = {value};\n" for key, value in settings.items())
            objects.append(f"\t\t{config_id} /* {name} */ = {{\n\t\t\tisa = XCBuildConfiguration;\n"
                           f"\t\t\tbuildSettings = {{\n{lines}\t\t\t}};\n\t\t\tname = {name};\n\t\t}};\n")
        objects.append(f"\t\t{list_id} /* Build configuration list for {owner} */ = {{\n"
                       f"\t\t\tisa = XCConfigurationList;\n\t\t\tbuildConfigurations = (\n{''.join(refs)}\t\t\t);\n"
                       "\t\t\tdefaultConfigurationName = Release;\n\t\t};\n")
        return list_id

    for name, extra in targets.items():
        settings = {"MARKETING_VERSION": marketing, "CURRENT_PROJECT_VERSION": build, **extra}
        list_id = config_list(f'PBXNativeTarget "{name}"', {"Debug": dict(settings), "Release": dict(settings)})
        objects.append(f"\t\t{next(ids)} /* {name} */ = {{\n\t\t\tisa = PBXNativeTarget;\n"
                       f"\t\t\tbuildConfigurationList = {list_id} /* Build configuration list */;\n"
                       f"\t\t\tname = {name};\n\t\t\tproductType = \"com.apple.product-type.application\";\n\t\t}};\n")
    project_list = config_list(f'PBXProject "{project_name}"', {"Debug": {"SDKROOT": "iphoneos"},
                                                                  "Release": {"SDKROOT": "iphoneos"}})
    objects.append(f"\t\t{next(ids)} /* Project object */ = {{\n\t\t\tisa = PBXProject;\n"
                   f"\t\t\tbuildConfigurationList = {project_list} /* Build configuration list */;\n\t\t}};\n")
    return "// !$*UTF8*$!\n{\n\tobjects = {\n" + "".join(objects) + "\t};\n}\n"


def info_plist(name: str, *, encryption: bool = False) -> bytes:
    return plistlib.dumps({"CFBundleName": name, "ITSAppUsesNonExemptEncryption": encryption})


def appstore_md(*, version: str) -> str:
    def block(heading: str, body: str) -> str:
        return f"## {heading}\n\n```\n{body}\n```\n\n"

    return ("# App Store listing (synthetic)\n\n"
            + block("App Name", "Synthetic App")
            + block("Subtitle (30 chars max)", "Track it with AI")
            + block("Promotional Text (170 chars max)", "A synthetic promotional text for tests.")
            + block("Keywords (100 chars max)", "food,tracker,calories")
            + "## Category\n\nHealth\n\n"
            + block("Description", "A synthetic description.\n\nIt has two paragraphs.")
            + block(f"What's New (v{version})", "• Faster logging\n- Fixes")
            + block("Privacy URL", "https://example.com/privacy")
            + block("Support URL", "https://example.com/support")
            + block("Marketing URL", "https://example.com"))


def playstore_md(*, version: str) -> str:
    def block(heading: str, body: str) -> str:
        return f"## {heading}\n\n```\n{body}\n```\n\n"

    return ("# Play listing (synthetic)\n\n"
            + block("1. App Name", "Synthetic App")
            + block("2. Short Description", "Track food with AI in seconds.")
            + block("3. Full Description", "A synthetic full description.")
            + block(f"4. What's New (v{version})", "<en-US>\n• Faster logging\n</en-US>\n<de-DE>\n• Schneller\n</de-DE>")
            + "## 5. Categorization\n\nHealth\n")


def gradle(*, version_name: str, version_code: int, package: str) -> str:
    return ("plugins {\n    id(\"com.android.application\")\n}\n\nandroid {\n"
            f"    namespace = \"{package}\"\n    defaultConfig {{\n        applicationId = \"{package}\"\n"
            f"        versionCode = {version_code}\n        versionName = \"{version_name}\"\n    }}\n"
            "    buildTypes {\n        debug {\n            applicationIdSuffix = \".debug\"\n"
            "            versionNameSuffix = \"-debug\"\n        }\n    }\n}\n")


def fud_ai_catalog() -> tuple[dict, dict]:
    def sub(plan: str, period: str) -> dict:
        return {"id": f"{PREFIX}.{plan}.{period}", "plan": plan, "entitlement": plan,
                "platforms": ["ios", "android"],
                "play": {"subscription_id": plan, "base_plan_id": period, "revenuecat_product_id": f"{plan}:{period}"}}

    products = {
        "$schema_comment": "Synthetic catalog shaped like Fud AI's.",
        "package": {"ios": PREFIX, "android": PREFIX},
        "subscriptions": [sub("plus", "monthly"), sub("plus", "yearly"), sub("pro", "monthly"), sub("pro", "yearly")],
        "consumables": [{"id": f"{PREFIX}.credits.{n}", "kind": "credits", "credits": n, "platforms": ["ios", "android"],
                         "play": {"product_id": f"credits_{n}"}} for n in (50, 150, 400)],
        "tips": [{"id": f"{PREFIX}.tip.{tier}", "tier": tier, "platforms": ["ios", "android"],
                  "play": {"product_id": f"tip_{tier}"}} for tier in ("snack", "proteinshake", "lunch", "feast")],
    }
    credits = [{"identifier": f"credits_{n}", "product_id": f"{PREFIX}.credits.{n}", "play_product_id": f"credits_{n}"}
               for n in (50, 150, 400)]
    revenuecat = {
        "entitlements": [
            {"id": "plus", "products": [f"{PREFIX}.plus.monthly", f"{PREFIX}.plus.yearly"],
             "play_products": ["plus:monthly", "plus:yearly"]},
            {"id": "pro", "includes_entitlements": ["plus"],
             "products": [f"{PREFIX}.pro.monthly", f"{PREFIX}.pro.yearly"],
             "play_products": ["pro:monthly", "pro:yearly"]},
        ],
        "offerings": [
            {"identifier": "plus", "packages": [
                {"identifier": "$rc_monthly", "product_id": f"{PREFIX}.plus.monthly", "play_product_id": "plus:monthly"},
                {"identifier": "$rc_annual", "product_id": f"{PREFIX}.plus.yearly", "play_product_id": "plus:yearly"},
                *credits]},
            {"identifier": "pro", "packages": [
                {"identifier": "pro_monthly", "product_id": f"{PREFIX}.pro.monthly", "play_product_id": "pro:monthly"},
                {"identifier": "pro_yearly", "product_id": f"{PREFIX}.pro.yearly", "play_product_id": "pro:yearly"},
                *credits]},
        ],
    }
    return products, revenuecat


STUB_WORKFLOW = "name: {name}\non:\n  workflow_call:\npermissions:\n  contents: read\njobs:\n  {job}:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"


def write_text(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@functools.lru_cache(maxsize=8)
def sized_rgb(width: int, height: int, size: int | None) -> bytes:
    """An RGB PNG, grown to exactly size bytes (when given) with a private chunk before IEND."""
    raw = pngs.rgb(width, height)
    if size is None:
        return raw
    filler = size - len(raw) - 12
    if filler < 0:
        raise ValueError(f"a {width}x{height} PNG is already {len(raw)} bytes, more than {size}")
    iend = len(raw) - 12
    return raw[:iend] + pngs.chunk(b"prVt", b"\0" * filler) + raw[iend:]


def creative(root: Path, folder: str = "store/creative/ios", *, sizes: dict[str, int] | None = None) -> None:
    """The two Fud AI images; with sizes (ios.creative.sizes), each exactly its pinned size."""
    sizes = sizes or {}
    for relative, (width, height) in (("header/fud-ai-header-orb-horizon.png", (3840, 1646)),
                                      ("search-results/fud-ai-search-voice.png", (3840, 2560))):
        pngs.write(root / folder / relative, sized_rgb(width, height, sizes.get(relative)))


def pinned_sizes(config_text: str) -> dict[str, int]:
    """ios.creative.sizes of a config text, or {} (also for text that is not valid TOML)."""
    try:
        data = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return {}
    sizes = ((data.get("ios") or {}).get("creative") or {}).get("sizes")
    return dict(sizes) if isinstance(sizes, dict) else {}


def fud_ai_screenshots(root: Path) -> None:
    for i in range(1, 11):
        pngs.write(root / "store/screenshots/ios" / f"{i:02d}-iphone.png", pngs.rgba(1290, 2796))
    for i in range(1, 8):
        pngs.write(root / "store/screenshots/ipad" / f"{i:02d}-ipad.png", pngs.rgb(2064, 2752))
        pngs.write(root / "store/screenshots/iphone-duo" / f"{i:02d}-duo.png", pngs.rgb(1398, 2034))
    pngs.write(root / "store/screenshots/watch" / "01-home-glance.png", pngs.rgba(416, 496))
    for i in range(1, 9):
        pngs.write(root / "store/screenshots/play" / f"{i:02d}-play.png", pngs.rgba(1290, 2796))


def write_fud_ai_tree(root: Path, *, screenshots: bool = False, config: str | None = None) -> Path:
    """Fud AI's shape: 7 targets x Debug/Release at 7.2 (39), listing docs, catalog, creative images."""
    root.mkdir(parents=True, exist_ok=True)
    text = config if config is not None else (EXAMPLES / "fud-ai" / "store-release.toml").read_text()
    write_text(root, "store/store-release.toml", text)
    targets = {name: {} for name in FUD_AI_TARGETS}
    targets["calorietracker"] = {"INFOPLIST_FILE": "calorietracker/Info.plist", "GENERATE_INFOPLIST_FILE": "YES"}
    write_text(root, "ios/calorietracker.xcodeproj/project.pbxproj",
               pbxproj(targets, project_name="calorietracker", marketing="7.2", build="39"))
    (root / "ios/calorietracker").mkdir(parents=True, exist_ok=True)
    (root / "ios/calorietracker/Info.plist").write_bytes(info_plist("Fud AI"))
    write_text(root, "APPSTORE.md", appstore_md(version="7.2"))
    write_text(root, "PLAYSTORE.md", playstore_md(version="7.2"))
    write_text(root, "RELEASE_NOTES.md", "# Release notes\n\n## Unreleased\n\nWork in progress.\n\n"
                                         "## ios-v7.2\n\nShipped.\n\n## android-v7.2\n\nBeta.\n")
    products, revenuecat = fud_ai_catalog()
    write_text(root, "store/catalog/products.json", json.dumps(products, indent=2) + "\n")
    write_text(root, "store/catalog/revenuecat.json", json.dumps(revenuecat, indent=2) + "\n")
    (root / "scripts/ci").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(UPSTREAM / "validate_catalog.py", root / "scripts/ci/check_catalog.py")
    write_text(root, "scripts/ci/ensure_metal_toolchain.sh", "#!/bin/bash\nset -euo pipefail\necho \"metal toolchain ok\"\n")
    write_text(root, "android/app/build.gradle.kts", gradle(version_name="7.2", version_code=42, package=PREFIX))
    write_text(root, ".github/workflows/ios-tests.yml", STUB_WORKFLOW.format(name="iOS tests", job="ios-tests"))
    write_text(root, ".github/workflows/quality.yml", STUB_WORKFLOW.format(name="Quality checks", job="web"))
    # The pinned sizes (ios.creative.sizes) are Fud AI's real ones, so the images are grown to them.
    creative(root, sizes=pinned_sizes(text))
    if screenshots:
        fud_ai_screenshots(root)
    return root


def write_new_app_tree(root: Path, *, generated_plist: bool = False) -> Path:
    """A new app before `run.py init`: ios/MyApp.xcodeproj (one MyApp target at 1.0 (1), its Info.plist or a
    generated one), android/app/build.gradle.kts and the two test workflows init names by default."""
    settings = ({"GENERATE_INFOPLIST_FILE": "YES", "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption": "NO"}
                if generated_plist else {"INFOPLIST_FILE": "MyApp/Info.plist"})
    write_text(root, "ios/MyApp.xcodeproj/project.pbxproj",
               pbxproj({"MyApp": settings}, project_name="MyApp", marketing="1.0", build="1"))
    if not generated_plist:
        (root / "ios/MyApp").mkdir(parents=True, exist_ok=True)
        (root / "ios/MyApp/Info.plist").write_bytes(info_plist("My App"))
    write_text(root, "android/app/build.gradle.kts", gradle(version_name="1.0", version_code=1, package="com.example.myapp"))
    write_text(root, ".github/workflows/ios-tests.yml", STUB_WORKFLOW.format(name="iOS tests", job="tests"))
    write_text(root, ".github/workflows/android-tests.yml", STUB_WORKFLOW.format(name="Android tests", job="tests"))
    return root


def write_example_tree(root: Path, *, screenshots: bool = True, config: str | None = None) -> Path:
    """The example app: one target at 1.0 (3), two App Store sets, Play, no catalog."""
    root.mkdir(parents=True, exist_ok=True)
    write_text(root, "store/store-release.toml",
               config if config is not None else (EXAMPLES / "example" / "store-release.toml").read_text())
    write_text(root, "Example.xcodeproj/project.pbxproj",
               pbxproj({"Example": {"INFOPLIST_FILE": "Example/Info.plist"}}, project_name="Example",
                       marketing="1.0", build="3"))
    (root / "Example").mkdir(parents=True, exist_ok=True)
    (root / "Example/Info.plist").write_bytes(info_plist("Example"))
    write_text(root, "APPSTORE.md", appstore_md(version="1.0"))
    write_text(root, "PLAYSTORE.md", playstore_md(version="1.0"))
    write_text(root, "RELEASE_NOTES.md", "# Notes\n\n## v1.0\n\nFirst.\n\n## android-v1.0\n\nFirst on Android.\n")
    write_text(root, "android/app/build.gradle.kts", gradle(version_name="1.0", version_code=3, package="com.example.app"))
    write_text(root, ".github/workflows/ios-tests.yml", STUB_WORKFLOW.format(name="iOS tests", job="tests"))
    write_text(root, ".github/workflows/android-tests.yml", STUB_WORKFLOW.format(name="Android tests", job="tests"))
    creative(root)
    if screenshots:
        for i in (1, 2):
            pngs.write(root / "store/screenshots/iphone" / f"{i:02d}-home.png", pngs.rgba(1290, 2796))
            pngs.write(root / "store/screenshots/ipad" / f"{i:02d}-home.png", pngs.rgb(2064, 2752))
            pngs.write(root / "store/screenshots/play" / f"{i:02d}-home.png", pngs.rgba(1290, 2796))
    return root
