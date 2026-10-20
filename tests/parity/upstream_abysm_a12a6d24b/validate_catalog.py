#!/usr/bin/env python3
"""Check store/catalog/products.json against the app's StoreKit file and StoreManager.

The catalog lists the App Store products a release may submit for review
(scripts/store/asc_release.py). It must match the product the app sells
(Abysm/StoreManager.swift) and the local StoreKit configuration (Abysm.storekit).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "store" / "catalog" / "products.json"
STOREKIT = ROOT / "Abysm.storekit"
STORE_MANAGER = ROOT / "Abysm" / "StoreManager.swift"
BUNDLE_ID = "com.apoorvdarshan.Abysm"
GROUPS = {"nonConsumables": "NonConsumable", "consumables": "Consumable", "subscriptions": "AutoRenewable"}


class CatalogError(Exception):
    pass


def swift_constant(source: str, name: str) -> str:
    match = re.search(rf'static let {name} = "([^"]+)"', source)
    if not match:
        raise CatalogError(f"StoreManager.swift has no `static let {name}`")
    return match.group(1)


def storekit_products(storekit: dict) -> dict[str, str]:
    products = {p["productID"]: p["type"] for p in storekit.get("products") or []}
    for group in storekit.get("subscriptionGroups") or []:
        for sub in group.get("subscriptions") or []:
            products[sub["productID"]] = "AutoRenewable"
    return products


def validate(catalog: dict, storekit: dict, store_manager_source: str) -> list[str]:
    if catalog.get("app") != BUNDLE_ID:
        raise CatalogError(f"catalog app must be {BUNDLE_ID}, got {catalog.get('app')!r}")
    unknown = set(catalog) - set(GROUPS) - {"app"}
    if unknown:
        raise CatalogError(f"unknown catalog groups: {', '.join(sorted(unknown))}")
    seen: dict[str, str] = {}
    for group, storekit_type in GROUPS.items():
        items = catalog.get(group)
        if not isinstance(items, list):
            raise CatalogError(f"catalog group {group!r} must be a list")
        for item in items:
            pid = item.get("id") if isinstance(item, dict) else None
            if not isinstance(pid, str) or not pid.startswith(BUNDLE_ID + "."):
                raise CatalogError(f"{group}: product id {pid!r} must start with {BUNDLE_ID}.")
            if pid in seen:
                raise CatalogError(f"product id {pid} is listed twice")
            seen[pid] = storekit_type
    sk = storekit_products(storekit)
    if set(sk) != set(seen):
        raise CatalogError(
            f"catalog products {sorted(seen)} differ from Abysm.storekit products {sorted(sk)}"
        )
    for pid, storekit_type in seen.items():
        if sk[pid] != storekit_type:
            raise CatalogError(f"{pid} is {sk[pid]} in Abysm.storekit but listed as {storekit_type}")
    product_id = swift_constant(store_manager_source, "productID")
    entitlement = swift_constant(store_manager_source, "entitlementID")
    if product_id not in seen:
        raise CatalogError(f"StoreManager sells {product_id}, which the catalog does not list")
    unlock = next(i for i in catalog["nonConsumables"] if i["id"] == product_id)
    if unlock.get("entitlement") != entitlement:
        raise CatalogError(
            f"{product_id} grants entitlement {entitlement!r} in StoreManager, "
            f"catalog says {unlock.get('entitlement')!r}"
        )
    return sorted(seen)


def main() -> None:
    try:
        ids = validate(
            json.loads(CATALOG.read_text(encoding="utf-8")),
            json.loads(STOREKIT.read_text(encoding="utf-8")),
            STORE_MANAGER.read_text(encoding="utf-8"),
        )
    except (CatalogError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"catalog OK: {', '.join(ids)}")


if __name__ == "__main__":
    main()
