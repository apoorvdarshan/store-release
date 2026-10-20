"""Parity cases 12-14: credential redaction, the catalog (generic rules plus Fud AI's hook) and the
RevenueCat gate. The catalog runs are subprocesses, so the hook's own output is compared too."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest

from store_release import catalog, redact
from support import apps as support_apps
from support.upstream import ParityCase, outcome

support_apps.configure_fud_ai_defaults()

TOOL_ROOT = support_apps.TOOL_ROOT
KEY = "TESTKEY0001"
ISSUER = "00000000-test-4000-8000-issuer000001"


class RedactionParityTests(ParityCase):
    def corpus(self, folder) -> None:
        dist = folder / "Fud.xcdistributionlogs"
        dist.mkdir(parents=True)
        (folder / "archive.log").write_text(f"-authenticationKeyID {KEY} -authenticationKeyIssuerID {ISSUER}\n"
                                            f"AuthKey_{KEY}.p8\n")
        (dist / "options.plist").write_bytes(b"bplist00\x00\x01" + KEY.encode() + b"\x00" + ISSUER.upper().encode())
        (dist / "upload.log").write_bytes(f"key {KEY}".encode("utf-16-le"))
        (dist / "upload-be.log").write_bytes(f"key {KEY.lower()}".encode("utf-16-be"))
        (folder / "clean.log").write_text("nothing to hide")
        outside = folder.parent / f"{folder.name}-outside.txt"
        outside.write_text(KEY)
        (folder / "link.txt").symlink_to(outside)

    def test_the_redaction_corpus(self) -> None:
        results = []
        for name, module in (("upstream", self.up.redact_credential_ids), ("package", redact)):
            folder = self.tmp / name
            self.corpus(folder)
            changed, removed = module.redact_folder(folder, {"ASC_KEY_ID": KEY, "ASC_ISSUER_ID": ISSUER})
            files = {path.relative_to(folder).as_posix(): path.read_bytes()
                     for path in sorted(folder.rglob("*")) if path.is_file()}
            results.append((changed, sorted(p.relative_to(folder).as_posix() for p in removed), files))
        self.assertEqual(results[1], results[0])


class CatalogParityTests(ParityCase):
    def run_both(self) -> tuple:
        products = self.app.root / "scripts/store/validate_catalog.py"
        upstream = subprocess.run([sys.executable, "-I", str(products)], capture_output=True, text=True)
        package = subprocess.run([sys.executable, "-I", str(TOOL_ROOT / "run.py"), "--app-root", str(self.app.root),
                                  "catalog", "check"], capture_output=True, text=True)
        return upstream, package

    def mutations(self) -> list[tuple[str, dict, dict]]:
        products = json.loads((self.app.root / "store/catalog/products.json").read_text())
        revenuecat = json.loads((self.app.root / "store/catalog/revenuecat.json").read_text())
        cases = [("unchanged", products, revenuecat)]

        def add(label, change_products=None, change_revenuecat=None):
            p, r = copy.deepcopy(products), copy.deepcopy(revenuecat)
            if change_products:
                change_products(p)
            if change_revenuecat:
                change_revenuecat(r)
            cases.append((label, p, r))

        add("missing group", lambda p: p.pop("tips"))
        add("empty id", lambda p: p["tips"][0].update(id=""))
        add("numeric id", lambda p: p["consumables"][0].update(id=5))
        add("wrong prefix", lambda p: p["tips"][0].update(id="com.other.tip.snack"))
        add("duplicate id", lambda p: p["tips"][1].update(id=p["tips"][0]["id"]))
        add("play block without android", lambda p: p["tips"][0].update(platforms=["ios"]))
        add("ios only, no play block", lambda p: (p["tips"][0].update(platforms=["ios"]), p["tips"][0].pop("play")))
        add("android without play block", lambda p: p["tips"][0].pop("play"))
        add("bad subscription id", lambda p: p["subscriptions"][0]["play"].update(subscription_id="Plus"))
        add("bad base plan", lambda p: p["subscriptions"][0]["play"].update(base_plan_id="_monthly"))
        add("revenuecat id mismatch", lambda p: p["subscriptions"][0]["play"].update(revenuecat_product_id="plus"))
        add("bad play product id", lambda p: p["tips"][0]["play"].update(product_id="Tip-Snack"))
        add("long play product id", lambda p: p["tips"][0]["play"].update(product_id="t" * 41))
        add("duplicate play id", lambda p: p["tips"][1]["play"].update(product_id=p["tips"][0]["play"]["product_id"]))
        add("unknown entitlement", None, lambda r: r["entitlements"][0].update(id="max"))
        add("entitlement unknown product", None, lambda r: r["entitlements"][0]["products"].append("com.x"))
        add("entitlement consumable", None, lambda r: r["entitlements"][0]["play_products"].append("credits_50"))
        add("no offerings", None, lambda r: r.update(offerings=[]))
        add("missing pro offering", None, lambda r: r["offerings"].pop())
        add("empty plus packages", None, lambda r: r["offerings"][0].update(packages=[]))
        add("offering unknown product", None, lambda r: r["offerings"][0]["packages"][0].update(product_id="com.x"))
        add("offering unknown play product", None,
            lambda r: r["offerings"][0]["packages"][0].update(play_product_id="plus:weekly"))
        add("missing credits package", None, lambda r: r["offerings"][1]["packages"].pop())
        add("missing pro package", None, lambda r: r["offerings"][1]["packages"].pop(0))
        add("package without identifier", None, lambda r: r["offerings"][0]["packages"][0].pop("identifier"))
        add("pro without plus", None, lambda r: r["entitlements"][1].pop("includes_entitlements"))
        add("offering without identifier", None,
            lambda r: r["offerings"].append({"packages": [{"identifier": "x", "product_id": products["tips"][0]["id"]}]}))
        add("extra group ignored", lambda p: p.update(bonus=[{"id": "anything"}]))
        add("tip without platforms", lambda p: (p["tips"][0].pop("platforms"), p["tips"][0].pop("play")))
        return cases

    def test_thirty_mutations(self) -> None:
        cases = self.mutations()
        self.assertGreaterEqual(len(cases), 30)
        for label, products, revenuecat in cases:
            (self.app.root / "store/catalog/products.json").write_text(json.dumps(products))
            (self.app.root / "store/catalog/revenuecat.json").write_text(json.dumps(revenuecat))
            upstream, package = self.run_both()
            with self.subTest(label=label):
                self.assertEqual(package.returncode == 0, upstream.returncode == 0, (upstream.stderr, package.stderr))
                if upstream.returncode != 0 and "Traceback" not in upstream.stderr:
                    # Whichever layer reports (the generic rules, or the hook followed by the CLI's
                    # "hook ... exited" line), the error text is fud-ai's own.
                    error = upstream.stderr.strip().splitlines()[-1]
                    self.assertIn(error, package.stderr.strip().splitlines())
                    if "hook scripts/ci/check_catalog.py exited" not in package.stderr:
                        self.assertEqual(package.stderr.strip(), error)
                if upstream.returncode == 0:
                    self.assertIn(upstream.stdout.strip(), package.stdout)


class RevenueCatParityTests(ParityCase):
    def test_unset_false_and_true(self) -> None:
        for value in (None, "false", "true", "1", "OFF"):
            env = {k: v for k, v in os.environ.items() if k != "STORE_SYNC_REVENUECAT"}
            if value is not None:
                env["STORE_SYNC_REVENUECAT"] = value
            from unittest import mock

            with mock.patch.dict(os.environ, env, clear=True):
                upstream = outcome(self.up.sync_revenuecat_catalog.main)
                package = outcome(catalog.revenuecat_gate)
            with self.subTest(value=value):
                self.assertEqual(package, upstream)


if __name__ == "__main__":
    unittest.main()
