#!/usr/bin/env python3
"""Product catalog checks and the RevenueCat sync gate.

`catalog check` runs the generic rules every app shares (each configured group
exists as a list; ids are non-empty strings with the configured prefix and
unique; with play_rules, the Play ids and base plans), then every app hook in
catalog.checks, in order, with a scrubbed environment. App-specific rules
(entitlements, offerings, packages) live in those hooks.

`revenuecat-gate` counts the catalog and fails closed when STORE_SYNC_REVENUECAT
is on: the RevenueCat write path is not implemented. An invalid value fails too.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from store_release import UNCONFIGURED, hooks, switches

ROOT: Path = UNCONFIGURED
PRODUCTS: Path | None = UNCONFIGURED
REVENUECAT: Path | None = UNCONFIGURED
GROUPS: tuple[str, ...] = UNCONFIGURED
ID_PREFIX: str = UNCONFIGURED
PLAY_RULES: bool = UNCONFIGURED
CHECKS: tuple[str, ...] = UNCONFIGURED

# Google Play: product ids are 1-40 chars of [a-z0-9_.] starting with a letter or
# digit; base plan ids are 1-63 chars of [a-z0-9-]. Both are permanent once created.
PLAY_PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9_.]{0,39}$")
PLAY_BASE_PLAN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SUBSCRIPTIONS_GROUP = "subscriptions"


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def play_ids(products: dict) -> set[str]:
    """Validates each item's `play` block and returns every RevenueCat-visible Play id."""
    seen: list[str] = []
    for group in GROUPS:
        for item in products[group]:
            pid = item["id"]
            play = item.get("play")
            if "android" not in item.get("platforms", []):
                if play is not None:
                    fail(f"{pid} has a play block but does not list android")
                continue
            if not isinstance(play, dict):
                fail(f"{pid} lists android but has no play block")
            if group == SUBSCRIPTIONS_GROUP:
                sub = play.get("subscription_id", "")
                base = play.get("base_plan_id", "")
                if not PLAY_PRODUCT_ID.match(sub):
                    fail(f"{pid}: invalid Play subscription_id {sub!r}")
                if not PLAY_BASE_PLAN_ID.match(base):
                    fail(f"{pid}: invalid Play base_plan_id {base!r}")
                if play.get("revenuecat_product_id") != f"{sub}:{base}":
                    fail(f"{pid}: revenuecat_product_id must be '{sub}:{base}'")
                seen.append(f"{sub}:{base}")
            else:
                product_id = play.get("product_id", "")
                if not PLAY_PRODUCT_ID.match(product_id):
                    fail(f"{pid}: invalid Play product_id {product_id!r}")
                seen.append(product_id)
    if len(seen) != len(set(seen)):
        fail(f"duplicate Play product ids in {PRODUCTS.name}")
    return set(seen)


def load_products() -> dict:
    products = json.loads(PRODUCTS.read_text())
    if not isinstance(products, dict):
        fail(f"{PRODUCTS.name} must hold a JSON object")
    return products


def check_generic() -> tuple[int, int | None]:
    """The rules every app shares. Returns (products, Play ids or None without play_rules)."""
    products = load_products()
    ids: list[str] = []
    for group in GROUPS:
        if group not in products or not isinstance(products[group], list):
            fail(f"missing products.{group}")
        for item in products[group]:
            pid = item.get("id") if isinstance(item, dict) else None
            if not pid or not isinstance(pid, str):
                fail(f"invalid id in {group}: {item!r}")
            if not pid.startswith(ID_PREFIX):
                fail(f"unexpected product id prefix: {pid}")
            ids.append(pid)
    if len(ids) != len(set(ids)):
        fail(f"duplicate product ids in {PRODUCTS.name}")
    play_count = len(play_ids(products)) if PLAY_RULES else None
    return len(ids), play_count


def check(env: dict[str, str] | None = None) -> None:
    if PRODUCTS is None:
        print("catalog: no [catalog] in store-release.toml; nothing to check")
        return
    count, play_count = check_generic()
    if play_count is None:
        print(f"ok: {count} products")
    else:
        print(f"ok: {count} products ({play_count} on Play)")
    if CHECKS:
        hooks.run(ROOT, CHECKS, env=env)


def revenuecat_gate() -> None:
    try:
        sync = switches.parse_switch("STORE_SYNC_REVENUECAT", os.environ.get("STORE_SYNC_REVENUECAT"))
    except switches.SwitchError as exc:
        fail(str(exc))
    if PRODUCTS is None:
        print("catalog: no [catalog] in store-release.toml")
        sources = "the app repository"
    else:
        products = load_products()
        product_ids = [item["id"] for group in GROUPS for item in products[group]]
        if REVENUECAT is not None:
            revenuecat = json.loads(REVENUECAT.read_text())
            print(
                f"catalog: {len(product_ids)} products, "
                f"{len(revenuecat['entitlements'])} entitlements, "
                f"{len(revenuecat['offerings'])} offerings"
            )
        else:
            print(f"catalog: {len(product_ids)} products")
        names = [PRODUCTS] + ([REVENUECAT] if REVENUECAT is not None else [])
        sources = ", ".join(_relative(path) for path in names)

    if not sync:
        print("dry-run only (STORE_SYNC_REVENUECAT is false) — no RevenueCat API calls")
        return

    print(
        "error: STORE_SYNC_REVENUECAT is enabled but RevenueCat write/sync is not "
        "implemented yet. Catalog stays source of truth in git "
        f"({sources}). "
        "Keep STORE_SYNC_REVENUECAT false until a write path ships.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)
