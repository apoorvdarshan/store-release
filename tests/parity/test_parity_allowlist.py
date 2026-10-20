"""The only differences from fud-ai a99591908 the parity suite allows, each with its own test:
D1 (the padded Play frame is validated), D7 (staging into --out), D12 (the called-workflow job
name), D14 (strict STORE_SYNC_REVENUECAT) and D16 (messages name config keys)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from store_release import catalog
from store_release import screenshots as ss
from store_release.android import release as android_release
from support import apps as support_apps
from support import pngs
from support.upstream import ParityCase, outcome, tree_files

support_apps.configure_fud_ai_defaults()

PILLOW = ss.try_import_pillow()


class AllowlistTests(ParityCase):
    @unittest.skipIf(PILLOW is None and not os.environ.get("STORE_RELEASE_TESTS_NEED_PILLOW"),
                     "needs Pillow (not installed)")
    def test_d1_the_padded_play_frame_is_validated(self) -> None:
        if PILLOW is None:
            self.fail("Pillow is required here (STORE_RELEASE_TESTS_NEED_PILLOW is set)")
        pngs.write(self.app.root / "store/screenshots/play/01-play.png", pngs.rgba(1290, 2796))

        def no_pad(Image, src, target_w, target_h, fill=(0, 0, 0, 255)):
            return src.copy()

        with mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"}):
            with mock.patch.object(self.up.stage_screenshots, "fit_and_pad_rgba", side_effect=no_pad):
                upstream = outcome(lambda: self.up.stage_screenshots.stage(dry_run=False, platform="play"))
            with mock.patch.object(ss, "fit_and_pad_rgba", side_effect=no_pad):
                ss.restage(self.tmp / "out")
                package = outcome(lambda: ss.stage(dry_run=False, platform="play"))
        self.assertEqual(upstream[0], "ok", "upstream staged a frame Play would refuse")
        self.assertEqual(package[:2], ("exit", 1))
        self.assertIn("onto 9:16 gave 1290x2796", package[3])

    def test_d7_staging_never_writes_into_the_app(self) -> None:
        pngs.write(self.app.root / "store/screenshots/play/01-play.png", pngs.rgb(1080, 1920))
        ss.restage(self.tmp / "out")
        outcome(lambda: ss.stage(dry_run=False, platform="play"))
        self.assertEqual(sorted(tree_files(self.tmp / "out")), ["play/phone/01-play.png"])
        self.assertFalse((self.app.root / "store/metadata/screenshots").exists())
        outcome(lambda: self.up.stage_screenshots.stage(dry_run=False, platform="play"))
        self.assertTrue((self.app.root / "store/metadata/screenshots/play/phone/01-play.png").is_file())

    def test_d12_the_called_release_job_is_recognised(self) -> None:
        jobs = {1: [{"name": "release / release",
                     "steps": [{"name": "Upload to Play (internal)", "conclusion": "success"}]}]}
        self.assertIsNone(self.up.android_release.earlier_upload_attempt(jobs.get, 2))
        self.assertEqual(android_release.earlier_upload_attempt(jobs.get, 2), 1)

    def test_d14_invalid_sync_values_fail(self) -> None:
        with mock.patch.dict(os.environ, {"STORE_SYNC_REVENUECAT": "maybe"}):
            upstream = outcome(self.up.sync_revenuecat_catalog.main)
            package = outcome(catalog.revenuecat_gate)
        self.assertEqual(upstream[0], "ok")
        self.assertIn("dry-run only", upstream[2])
        self.assertEqual(package[:2], ("exit", 1))
        self.assertIn("STORE_SYNC_REVENUECAT='maybe' is not a valid switch value", package[3])

    def test_d16_messages_name_the_config_key(self) -> None:
        upstream = outcome(lambda: self.up.android_release.check_upload_certificate("0" * 64, "1" * 64))
        package = outcome(lambda: android_release.check_upload_certificate("0" * 64, "1" * 64))
        self.assertIn("update UPLOAD_CERT_SHA256 only after", upstream[1])
        self.assertIn("update android.upload_cert_sha256 only after", package[1])
        self.assertEqual(package[1].replace("android.upload_cert_sha256", "UPLOAD_CERT_SHA256"), upstream[1])


if __name__ == "__main__":
    unittest.main()
