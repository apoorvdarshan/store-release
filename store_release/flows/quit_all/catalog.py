"""Quit All's catalog check: products.json against PaywallManager.swift and Products.storekit.

Quit All ran these comparisons as a unit test (test_ios_release.py
RepoInputTests.test_product_catalog_matches_the_app_and_storekit_file) on every pull request
and in every tag run's prepare. They are the same comparisons, as a command:

  - the catalog's product ids (asc_release.intended_product_ids) equal the `case x = "com.quitall..."`
    product ids in PaywallManager.swift, and every product id in Products.storekit
  - the catalog's "entitlements" equal PaywallManager's `entitlementIDs = [...]`
  - the catalog's app.bundle_id and app.asc_app_id are this config's ios.bundle_id and ios.asc_app_id

The test also pinned today's three product ids; that pin states the catalog's content, not a
rule, and is not carried over.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def check(config) -> list[str]:
    from store_release.flows.quit_all import asc_release  # noqa: PLC0415

    root = Path(asc_release.ROOT)
    catalog = json.loads((root / config.catalog.path).read_text(encoding="utf-8"))
    catalog_ids = asc_release.intended_product_ids()
    paywall = (root / config.catalog.products_source).read_text(encoding="utf-8")
    swift_ids = set(re.findall(r'case \w+ = "(com\.quitall\.[^"]+)"', paywall))
    if swift_ids != catalog_ids:
        fail(f"the catalog's products {sorted(catalog_ids)} differ from SubscriptionProduct in "
             f"{config.catalog.products_source} {sorted(swift_ids)}")
    storekit = json.loads((root / config.catalog.storekit).read_text(encoding="utf-8"))
    storekit_ids = {p["productID"] for p in storekit["products"]} | {
        s["productID"] for group in storekit["subscriptionGroups"] for s in group["subscriptions"]
    }
    if storekit_ids != catalog_ids:
        fail(f"the catalog's products {sorted(catalog_ids)} differ from {config.catalog.storekit} "
             f"{sorted(storekit_ids)}")
    found = re.findall(r'entitlementIDs = \[([^\]]+)\]', paywall)
    if not found:
        fail(f"{config.catalog.products_source} has no entitlementIDs = [...]")
    entitlements = set(found[0].replace('"', "").split(", "))
    if entitlements != set(catalog.get("entitlements") or []):
        fail(f"the catalog's entitlements {sorted(catalog.get('entitlements') or [])} differ from PaywallManager's "
             f"entitlementIDs {sorted(entitlements)}")
    app = catalog.get("app") or {}
    if app.get("bundle_id") != config.ios.bundle_id:
        fail(f"the catalog's app.bundle_id is {app.get('bundle_id')!r}, not {config.ios.bundle_id}")
    if app.get("asc_app_id") != config.ios.asc_app_id:
        fail(f"the catalog's app.asc_app_id is {app.get('asc_app_id')!r}, not {config.ios.asc_app_id}")
    print(f"catalog OK: {len(catalog_ids)} products match {config.catalog.products_source} and "
          f"{config.catalog.storekit}")
    return sorted(catalog_ids)
