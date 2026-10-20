"""Ported release flows: each app's own store scripts, moved here unchanged except for configuration.

A flow is one app's pipeline logic (release gates, build-number choice, the build
wait, the App Store Connect preflight and store step, the listing export, and for
Verceltics the Play publisher), copied from that app's repository at the commit
docs/provenance.md names. The only edits are mechanical (store_release.flows.vendor
documents them and the port tests prove it): relative imports instead of sys.path,
and ROOT, which configure() sets from the app's store-release.toml together with
every app constant (bundle id, App Store Connect app id, tag prefix, paths).
Everything else, including each app's switch rules, version states, draft rules,
product route, screenshot layout and messages, is exactly what that app ran.

`ios.flow` / `android.flow` in store-release.toml selects one; "fud-ai" (the
default) is store-release's own modules (store_release.ios, store_release.android).
"""
from __future__ import annotations

from pathlib import Path

# Never a real folder: a flow module used before configure() fails on its first file access.
UNCONFIGURED_ROOT = Path("/store-release-flow-not-configured")

IOS_FLOWS = ("fud-ai", "scowld", "verceltics", "abysm", "pcm-notes", "quit-all")
ANDROID_FLOWS = ("fud-ai", "verceltics")
# flow name -> package under store_release.flows
PACKAGES = {"scowld": "scowld", "verceltics": "verceltics", "abysm": "abysm", "pcm-notes": "pcm_notes",
            "quit-all": "quit_all"}
UNCONFIGURED_TEXT = "store-release-flow-not-configured"


def configure(config, app_root: Path) -> list[str]:
    """Configure the flow(s) the config selects; returns the flow packages configured."""
    import importlib  # noqa: PLC0415

    flows = []
    if config.ios is not None and config.ios.flow != "fud-ai":
        flows.append(config.ios.flow)
    if config.android is not None and config.android.flow != "fud-ai" and config.android.flow not in flows:
        flows.append(config.android.flow)
    configured = []
    for flow in flows:
        package = importlib.import_module(f"store_release.flows.{PACKAGES[flow]}")
        package.configure(config, Path(app_root).resolve())
        configured.append(PACKAGES[flow])
    return configured


def flow_package(name: str):
    """The package of a ported flow (store_release.flows.<package>)."""
    import importlib  # noqa: PLC0415

    return importlib.import_module(f"store_release.flows.{PACKAGES[name]}")


def require(condition: bool, message: str) -> None:
    from store_release.config import ConfigError  # noqa: PLC0415

    if not condition:
        raise ConfigError(message)


def set_dirs(config, flow: str) -> dict[str, str]:
    """display type -> the configured folder of each screenshot set (the WATCH folder included)."""
    sets = config.ios.screenshot_sets or ()
    return {s.display_type: s.dir for s in sets}


def check_catalog_groups(config, flow: str, groups: tuple[str, ...]) -> None:
    catalog = config.catalog
    require(catalog is not None, f"the {flow} flow submits catalog products: add [catalog]")
    require(tuple(catalog.groups) == tuple(groups),
            f"catalog.groups: the {flow} flow reads exactly {list(groups)} (in this order)")
