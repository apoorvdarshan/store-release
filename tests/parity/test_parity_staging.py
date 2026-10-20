"""Parity case 7: screenshot staging. Upstream stages into store/metadata/screenshots/ in the
app; the package stages into --out. Layouts are compared after mapping one onto the other."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from store_release import screenshots as ss
from support import apps as support_apps
from support import pngs
from support.upstream import ParityCase, outcome, tree_files

support_apps.configure_fud_ai_defaults()

PILLOW = ss.try_import_pillow()


class StagingParityTests(ParityCase):
    def frames(self, *, watch: bool = True, play_count: int = 2, play_size=(1290, 2796), alpha: bool = True) -> None:
        shots = self.app.root / "store/screenshots"
        frame = pngs.rgba if alpha else pngs.rgb
        for i in (1, 2):
            pngs.write(shots / "ios" / f"{i:02d}-iphone.png", frame(1290, 2796))
        pngs.write(shots / "ios" / "03-iphone-69.png", pngs.rgb(1320, 2868))
        pngs.write(shots / "ipad" / "01-ipad.png", pngs.rgb(2064, 2752))
        pngs.write(shots / "iphone-duo" / "01-duo.png", pngs.rgb(1398, 2034))
        pngs.write(shots / "iphone-duo" / "02-inner.png", pngs.rgb(2007, 2853))
        if watch:
            pngs.write(shots / "watch" / "01-glance.png", frame(416, 496))
        for i in range(1, play_count + 1):
            pngs.write(shots / "play" / f"{i:02d}-play.png", frame(*play_size))

    def stage_both(self, *, dry_run: bool, platform: str, uploads: bool, pillow) -> None:
        upstream_dir = self.app.root / "store/metadata/screenshots"
        package_dir = self.tmp / "out"
        env = {"STORE_UPLOAD_SCREENSHOTS": "true" if uploads else ""}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(self.up.stage_screenshots, "try_import_pillow", return_value=pillow):
                upstream = outcome(lambda: self.up.stage_screenshots.stage(dry_run=dry_run, platform=platform))
            with mock.patch.object(ss, "try_import_pillow", return_value=pillow):
                ss.restage(package_dir)
                package = outcome(lambda: ss.stage(dry_run=dry_run, platform=platform))
        relative = upstream_dir.relative_to(self.app.root).as_posix()

        def mapped(result, base):
            kind, value, out, err = result
            if isinstance(value, list):
                value = [p.relative_to(base).as_posix() for p in value]
            return kind, value, out.replace(str(base), relative), err.replace(str(base), relative)

        self.assertEqual(mapped(package, package_dir), mapped(upstream, upstream_dir))
        self.assertEqual(tree_files(package_dir), tree_files(upstream_dir))

    def test_dry_run_and_raw_copies(self) -> None:
        self.frames()
        for platform in ("ios", "play", "all"):
            with self.subTest(platform=platform, dry_run=True):
                self.stage_both(dry_run=True, platform=platform, uploads=False, pillow=None)
            with self.subTest(platform=platform, dry_run=False):
                self.stage_both(dry_run=False, platform=platform, uploads=False, pillow=None)

    def test_uploads_without_pillow(self) -> None:
        self.frames(alpha=False, play_size=(1080, 1920))
        self.stage_both(dry_run=False, platform="all", uploads=True, pillow=None)
        self.frames(alpha=True)  # alpha and off-aspect frames are refused without Pillow
        self.stage_both(dry_run=False, platform="ios", uploads=True, pillow=None)
        self.stage_both(dry_run=False, platform="play", uploads=True, pillow=None)

    @unittest.skipIf(PILLOW is None and not os.environ.get("STORE_RELEASE_TESTS_NEED_PILLOW"),
                     "needs Pillow (not installed)")
    def test_uploads_with_pillow(self) -> None:
        if PILLOW is None:
            self.fail("Pillow is required here (STORE_RELEASE_TESTS_NEED_PILLOW is set)")
        self.frames()
        for platform in ("ios", "play", "all"):
            with self.subTest(platform=platform):
                self.stage_both(dry_run=False, platform=platform, uploads=True, pillow=PILLOW)

    def test_a_missing_watch_folder_and_the_play_cap(self) -> None:
        self.frames(watch=False, play_count=10, play_size=(1080, 1920), alpha=False)
        stale = self.app.root / "store/metadata/screenshots/ios/watch/01-stale.png"
        pngs.write(stale, pngs.rgb(416, 496))
        pngs.write(self.tmp / "out/ios/watch/01-stale.png", pngs.rgb(416, 496))
        self.stage_both(dry_run=False, platform="all", uploads=False, pillow=None)
        self.assertFalse(stale.exists())



if __name__ == "__main__":
    unittest.main()
