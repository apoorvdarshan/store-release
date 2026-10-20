"""Staging additions: the padded Play frame is validated (D1), --out staging, configured sets."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release import screenshots as ss
from support import apps as support_apps
from support import pngs

support_apps.configure_fud_ai_defaults()

PILLOW = ss.try_import_pillow()
NEED_PILLOW = os.environ.get("STORE_RELEASE_TESTS_NEED_PILLOW")


def run_main(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["screenshots stage", *args]), redirect_stdout(out), redirect_stderr(err):
        try:
            ss.main()
        except SystemExit as exc:
            return exc.code, out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.png"))


class OutLayoutTests(unittest.TestCase):
    """The CLI stages into --out, never into the app checkout."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def fud_ai(self) -> support_apps.AppContext:
        app = support_apps.fixture_app("fud-ai", self.tmp / "app")
        for folder, size in (("ios", (1290, 2796)), ("ipad", (2064, 2752)), ("iphone-duo", (1398, 2034)),
                             ("watch", (416, 496)), ("play", (1080, 1920))):
            pngs.write(app.root / "store/screenshots" / folder / "01-home.png", pngs.rgb(*size))
        return app

    def test_every_set_lands_under_out(self) -> None:
        app = self.fud_ai()
        out = self.tmp / "staged"
        with support_apps.configured(app), mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"}):
            code, stdout, err = run_main("--platform", "all", "--out", str(out))
        self.assertEqual(code, 0, err)
        for folder in ("ios/6.7", "ios/ipad-13", "ios/iphone-duo", "ios/watch", "play/phone"):
            with self.subTest(folder=folder):
                self.assertEqual(names(out / folder), ["01-home.png"])
        self.assertFalse((app.root / "store/metadata").exists(), "nothing is staged into the app checkout")
        self.assertIn("staged 1 Play phone + 1 ASC 6.7\" iPhone", stdout)

    def test_dry_run_shows_paths_outside_the_app(self) -> None:
        app = self.fud_ai()
        out = self.tmp / "staged"
        with support_apps.configured(app):
            code, stdout, err = run_main("--dry-run", "--out", str(out))
        self.assertEqual(code, 0, err)
        self.assertIn(f"→ {out / 'play' / 'phone'}", stdout)
        self.assertIn(f"→ {out / 'ios' / '6.7'}", stdout)
        self.assertFalse(out.exists())

    def test_the_example_app_stages_only_its_sets(self) -> None:
        app = support_apps.fixture_app("example", self.tmp / "example")
        out = self.tmp / "staged"
        pngs.write(out / "ios/watch/01-stale.png", pngs.rgb(416, 496))
        pngs.write(out / "ios/iphone-duo/01-other.png", pngs.rgb(1398, 2034))
        with support_apps.configured(app):
            self.assertIsNone(ss.watch_set())
            self.assertEqual([s.label for s in ss.ASC_SETS], ['6.7" iPhone', '13" iPad'])
            code, _, err = run_main("--platform", "ios", "--out", str(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(names(out / "ios/6.7"), ["01-home.png", "02-home.png"])
        self.assertEqual(names(out / "ios/ipad-13"), ["01-home.png", "02-home.png"])
        self.assertEqual(names(out / "ios/watch"), [], "stale Watch frames never reach an upload")
        self.assertEqual(names(out / "ios/iphone-duo"), ["01-other.png"], "an unconfigured set is not touched")

    def test_a_platform_without_config_fails(self) -> None:
        app = self.fud_ai()
        with support_apps.configured(app):
            ss.ASC_SETS = ()
            code, _, err = run_main("--platform", "ios", "--out", str(self.tmp / "x"))
            self.assertEqual(code, 1)
            self.assertIn("no [[ios.screenshots.sets]]", err)
            ss.PLAY_SOURCE = None
            code, _, err = run_main("--platform", "play", "--out", str(self.tmp / "x"))
            self.assertEqual(code, 1)
            self.assertIn("no [android.screenshots]", err)


@unittest.skipIf(PILLOW is None and not NEED_PILLOW, "needs Pillow (not installed)")
class PaddedPlayFrameTests(unittest.TestCase):
    """D1: an off-aspect Play frame is padded onto 9:16 first, then the padded size is validated."""

    needs_pillow = True

    def setUp(self) -> None:
        if PILLOW is None:
            self.fail("Pillow is required here (STORE_RELEASE_TESTS_NEED_PILLOW is set)")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.app = support_apps.fixture_app("fud-ai", self.tmp / "app")
        self.source = self.app.root / "store/screenshots/play"
        pngs.write(self.source / "01-home.png", pngs.rgba(1290, 2796))

    def stage_play(self) -> tuple[int, str, str]:
        with support_apps.configured(self.app), mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"}):
            return run_main("--platform", "play", "--out", str(self.tmp / "staged"))

    def test_fud_ais_frames_pad_to_a_valid_size(self) -> None:
        code, _, err = self.stage_play()
        self.assertEqual(code, 0, err)
        with PILLOW.open(self.tmp / "staged/play/phone/01-home.png") as img:
            self.assertEqual(img.size, (1573, 2796))
        self.assertTrue(ss.play_portrait_ok(1573, 2796))
        self.assertFalse(ss.play_portrait_ok(1290, 2796), "the raw frame is not 9:16, so it is padded")

    def test_a_bad_padded_size_fails(self) -> None:
        def no_pad(Image, src, target_w, target_h, fill=(0, 0, 0, 255)):
            return src.copy()

        with mock.patch.object(ss, "fit_and_pad_rgba", side_effect=no_pad):
            code, _, err = self.stage_play()
        self.assertEqual(code, 1)
        self.assertIn("01-home.png: padding 1290x2796 onto 9:16 gave 1290x2796", err)
        self.assertEqual(names(self.tmp / "staged/play/phone"), [])


if __name__ == "__main__":
    unittest.main()
