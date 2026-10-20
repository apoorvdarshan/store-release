#!/usr/bin/env python3
"""RevenueCat catalog sync gate. Write path is not implemented — fail closed when enabled."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = ROOT / "store" / "catalog" / "products.json"
REVENUECAT = ROOT / "store" / "catalog" / "revenuecat.json"


def enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    products = json.loads(PRODUCTS.read_text())
    revenuecat = json.loads(REVENUECAT.read_text())
    product_ids = [
        item["id"]
        for group in ("subscriptions", "consumables", "tips")
        for item in products[group]
    ]

    print(
        f"catalog: {len(product_ids)} products, "
        f"{len(revenuecat['entitlements'])} entitlements, "
        f"{len(revenuecat['offerings'])} offerings"
    )

    if not enabled("STORE_SYNC_REVENUECAT"):
        print("dry-run only (STORE_SYNC_REVENUECAT is false) — no RevenueCat API calls")
        return

    print(
        "error: STORE_SYNC_REVENUECAT is enabled but RevenueCat write/sync is not "
        "implemented yet. Catalog stays source of truth in git "
        "(store/catalog/products.json, store/catalog/revenuecat.json). "
        "Keep STORE_SYNC_REVENUECAT false until a write path ships.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
