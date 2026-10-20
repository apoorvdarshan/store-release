"""Offline tests for screenshot staging: per-platform sources, cleanup, order and caps.

Ported from fud-ai a99591908 scripts/store/test_stage_screenshots.py (imports, the
Pillow switch name and the needs_pillow markers changed).
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from store_release import screenshots as ss
from store_release.ios import asc as asc_release
from support import apps as support_apps

support_apps.configure_fud_ai_defaults()

# Store checks run these again after installing Pillow, with
# STORE_RELEASE_TESTS_NEED_PILLOW set, so the resizing tests can't skip there.
PILLOW = ss.try_import_pillow()


def png(width: int, height: int, marker: str) -> bytes:
    # Signature + IHDR length/type + size: enough for read_png_size without Pillow.
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + marker.encode()
    )


class SetsFixture(unittest.TestCase):
    """The real screenshot sets under a temporary root. PILLOW picks the staging path."""

    PILLOW = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # The real sets, moved under the temporary root.
        self.asc_sets = [
            ss.AscSet(
                asc_set.label,
                self.root / asc_set.source.relative_to(ss.ROOT),
                self.root / asc_set.dest.relative_to(ss.ROOT),
                asc_set.accepted,
            )
            for asc_set in ss.ASC_SETS
        ]
        self.ios_src, self.ipad_src, self.duo_src = (a.source for a in self.asc_sets)
        self.ios_dest, self.ipad_dest, self.duo_dest = (a.dest for a in self.asc_sets)
        self.play_src = self.root / "store" / "screenshots" / "play"
        self.play_dest = self.root / "store" / "metadata" / "screenshots" / "play" / "phone"
        for patch in (
            mock.patch.object(ss, "ROOT", self.root),
            mock.patch.object(ss, "ASC_SETS", tuple(self.asc_sets)),
            mock.patch.object(ss, "PLAY_SOURCE", self.play_src),
            mock.patch.object(ss, "PLAY_PHONE_DIR", self.play_dest),
            mock.patch.object(ss, "try_import_pillow", return_value=self.PILLOW),
            mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": ""}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, directory: Path, *names: str, size: tuple[int, int] = (1290, 2796)) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_bytes(png(*size, f"{directory.name}/{name}"))

    def write_tablet_sets(self) -> None:
        self.write(self.ipad_src, "01-i.png", size=(2064, 2752))
        self.write(self.duo_src, "01-d.png", size=(1398, 2034))

    def stage(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return ss.stage(**kwargs)

    def names(self, directory: Path) -> list[str]:
        return sorted(p.name for p in directory.glob("*.png"))


class StageTests(SetsFixture):
    # Raw copies, so staged bytes can be compared with the sources.
    PILLOW = None

    def test_all_stages_each_store_from_its_own_source_in_name_order(self):
        self.write(self.ios_src, "02-b.png", "10-j.png", "01-a.png", "home.png")
        self.write_tablet_sets()
        self.write(self.play_src, "02-y.png", "01-x.png")

        copied = self.stage(dry_run=False, platform="all")

        self.assertEqual(self.names(self.ios_dest), ["01-a.png", "02-b.png", "10-j.png"])
        self.assertEqual(self.names(self.ipad_dest), ["01-i.png"])
        self.assertEqual(self.names(self.duo_dest), ["01-d.png"])
        self.assertEqual(self.names(self.play_dest), ["01-x.png", "02-y.png"])
        self.assertEqual(
            [p.name for p in copied],
            ["01-x.png", "02-y.png", "01-a.png", "02-b.png", "10-j.png", "01-i.png", "01-d.png"],
        )
        for dest, src in (
            (self.ios_dest, self.ios_src),
            (self.ipad_dest, self.ipad_src),
            (self.duo_dest, self.duo_src),
            (self.play_dest, self.play_src),
        ):
            for name in self.names(dest):
                self.assertEqual((dest / name).read_bytes(), (src / name).read_bytes())

    def test_ios_only_replaces_ios_set_and_keeps_staged_play_files(self):
        self.write(self.ios_src, "01-a.png")
        self.write_tablet_sets()
        self.write(self.ios_dest, "09-stale.png")
        self.write(self.ipad_dest, "09-stale.png")
        self.write(self.play_dest, "01-staged.png")

        self.stage(dry_run=False, platform="ios")

        self.assertEqual(self.names(self.ios_dest), ["01-a.png"])
        self.assertEqual(self.names(self.ipad_dest), ["01-i.png"])
        self.assertEqual(self.names(self.play_dest), ["01-staged.png"])

    def test_play_only_replaces_play_set_and_keeps_staged_ios_files(self):
        self.write(self.play_src, "01-x.png")
        self.write(self.play_dest, "09-stale.png")
        self.write(self.ios_dest, "01-staged.png")

        self.stage(dry_run=False, platform="play")

        self.assertEqual(self.names(self.play_dest), ["01-x.png"])
        self.assertEqual(self.names(self.ios_dest), ["01-staged.png"])

    def test_one_platform_does_not_need_the_other_source(self):
        self.write(self.ios_src, "01-a.png")
        self.write_tablet_sets()

        self.stage(dry_run=False, platform="ios")

        self.assertEqual(self.names(self.ios_dest), ["01-a.png"])
        self.assertFalse(self.play_dest.exists())

    def test_play_stages_at_most_the_phone_cap(self):
        names = [f"{i:02d}-p.png" for i in range(1, ss.PLAY_PHONE_MAX + 2)]
        self.write(self.play_src, *names)

        self.stage(dry_run=False, platform="play")

        self.assertEqual(self.names(self.play_dest), names[: ss.PLAY_PHONE_MAX])

    def test_dry_run_writes_and_removes_nothing(self):
        self.write(self.ios_src, "01-a.png")
        self.write_tablet_sets()
        self.write(self.play_src, "01-x.png")
        self.write(self.ios_dest, "09-stale.png")

        copied = self.stage(dry_run=True, platform="all")

        self.assertEqual(copied, [])
        self.assertEqual(self.names(self.ios_dest), ["09-stale.png"])
        self.assertFalse(self.play_dest.exists())

    def test_missing_or_empty_source_fails_before_touching_staged_files(self):
        self.write(self.ios_dest, "01-staged.png")
        self.write_tablet_sets()
        for setup in (lambda: None, lambda: self.write(self.ios_src, "home.png")):
            setup()
            with self.subTest(source_files=self.names(self.ios_src) if self.ios_src.exists() else None):
                with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                    self.stage(dry_run=False, platform="ios")
                self.assertEqual(self.names(self.ios_dest), ["01-staged.png"])

    def test_every_ios_set_needs_its_own_source(self):
        self.write(self.ios_src, "01-a.png")
        self.write(self.ios_dest, "01-staged.png")
        for source in (self.ipad_src, self.duo_src):
            self.write_tablet_sets()
            for stale in source.glob("*.png"):
                stale.unlink()
            with self.subTest(source=source.name):
                with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                    self.stage(dry_run=False, platform="ios")
                self.assertEqual(self.names(self.ios_dest), ["01-staged.png"])

    def test_a_frame_from_another_devices_set_fails_even_with_uploads_off(self):
        self.write(self.ios_src, "01-a.png")
        self.write_tablet_sets()
        self.write(self.ipad_dest, "01-staged.png")
        for source, size in ((self.ipad_src, (1290, 2796)), (self.duo_src, (2064, 2752))):
            self.write_tablet_sets()
            self.write(source, "02-misplaced.png", size=size)
            with self.subTest(source=source.name):
                for dry_run in (True, False):
                    stderr = io.StringIO()
                    with self.assertRaises(SystemExit), redirect_stderr(stderr):
                        self.stage(dry_run=dry_run, platform="ios")
                    self.assertIn("02-misplaced.png", stderr.getvalue())
                self.assertEqual(self.names(self.ipad_dest), ["01-staged.png"])
            (source / "02-misplaced.png").unlink()

    def test_a_frame_shaped_like_its_set_is_still_staged(self):
        # 6.9" iPhone frames have the 6.7" shape; staging fits them onto 1290x2796.
        self.assertTrue(ss.asc_frame_fits(self.asc_sets[0], 1320, 2868))
        self.write(self.ios_src, "01-a.png", size=(1320, 2868))
        self.write_tablet_sets()

        self.stage(dry_run=False, platform="ios")

        self.assertEqual(self.names(self.ios_dest), ["01-a.png"])

    def test_upload_without_pillow_checks_each_sets_sizes(self):
        self.write(self.ios_src, "01-a.png")
        self.write_tablet_sets()
        # Both Duo screens fit the one Duo set.
        self.write(self.duo_src, "02-inner.png", size=(2007, 2853))
        with mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"}):
            self.stage(dry_run=False, platform="ios")
            self.assertEqual(self.names(self.duo_dest), ["01-d.png", "02-inner.png"])

            # An iPhone-sized frame in the iPad set is refused before anything is replaced.
            self.write(self.ipad_src, "02-phone.png")
            stderr = io.StringIO()
            with self.assertRaises(SystemExit), redirect_stderr(stderr):
                self.stage(dry_run=False, platform="ios")
        self.assertIn('13" iPad', stderr.getvalue())
        self.assertEqual(self.names(self.ipad_dest), ["01-i.png"])


@unittest.skipIf(
    PILLOW is None and not os.environ.get("STORE_RELEASE_TESTS_NEED_PILLOW"), "needs Pillow (not installed)"
)
class PillowStageTests(SetsFixture):
    """The path release runs take: Pillow keeps accepted sizes and fits the rest."""

    needs_pillow = True
    PILLOW = PILLOW

    def setUp(self):
        if self.PILLOW is None:
            self.fail("Pillow is required here (STORE_RELEASE_TESTS_NEED_PILLOW is set)")
        super().setUp()

    def image(self, directory: Path, name: str, size: tuple[int, int], color) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        self.PILLOW.new("RGBA", size, color).save(path)
        return path

    def staged_size(self, path: Path) -> tuple[int, int]:
        with self.PILLOW.open(path) as img:
            return img.size

    def test_accepted_sizes_stay_unchanged_and_other_shapes_fit_their_set(self):
        cases = [
            # (set index, source size, staged size)
            (0, (1290, 2796), (1290, 2796)),
            (0, (1284, 2778), (1284, 2778)),
            (0, (1320, 2868), (1290, 2796)),
            (1, (2064, 2752), (2064, 2752)),
            (1, (2048, 2732), (2048, 2732)),
            (1, (1032, 1376), (2064, 2752)),
            (2, (1398, 2034), (1398, 2034)),
            (2, (2007, 2853), (2007, 2853)),
            (2, (699, 1017), (1398, 2034)),
        ]
        sources = []
        for i, (index, size, _) in enumerate(cases):
            asc_set = self.asc_sets[index]
            sources.append(self.image(asc_set.source, f"{i + 1:02d}-frame.png", size, (255, 83, 142, 255)))

        with mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"}):
            self.stage(dry_run=False, platform="ios")

        for source, (index, size, staged) in zip(sources, cases):
            dest = self.asc_sets[index].dest / source.name
            with self.subTest(set=self.asc_sets[index].label, size=size):
                self.assertEqual(self.staged_size(dest), staged)
                if size == staged:
                    with self.PILLOW.open(source) as a, self.PILLOW.open(dest) as b:
                        self.assertEqual(a.convert("RGBA").tobytes(), b.convert("RGBA").tobytes())


def png_file(width: int, height: int, colour_type: int, *chunks: tuple[bytes, bytes]) -> bytes:
    """A PNG with real chunk framing (IHDR, extra chunks, an empty IDAT, IEND), read without Pillow."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, colour_type, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + b"".join(chunk(k, d) for k, d in chunks)
            + chunk(b"IDAT", b"") + chunk(b"IEND", b""))


def chunk_types(raw: bytes) -> list[bytes]:
    return [kind for kind, _ in asc_release.png_chunks(raw)]


class WatchStageTests(SetsFixture):
    """The optional Apple Watch set (raw copies, no Pillow)."""

    PILLOW = None

    def setUp(self):
        super().setUp()
        self.watch = ss.watch_set()
        self.write(self.ios_src, "01-a.png")
        self.write_tablet_sets()

    def test_watch_frames_are_staged_when_the_folder_exists(self):
        self.assertEqual(self.watch.source, self.root / "store" / "screenshots" / "watch")
        self.assertEqual(self.watch.dest, self.root / "store" / "metadata" / "screenshots" / "ios" / "watch")
        self.write(self.watch.source, "01-glance.png", "02-ultra.png", "notes.png", size=(416, 496))
        self.write(self.watch.source, "02-ultra.png", size=(422, 514))

        copied = self.stage(dry_run=False, platform="ios")

        self.assertEqual(self.names(self.watch.dest), ["01-glance.png", "02-ultra.png"])
        self.assertEqual([p.name for p in copied][-2:], ["01-glance.png", "02-ultra.png"])
        for name in self.names(self.watch.dest):
            self.assertEqual((self.watch.dest / name).read_bytes(), (self.watch.source / name).read_bytes())
        # Every size App Store Connect takes for a Watch set is accepted as it is.
        self.assertEqual(set(self.watch.accepted), set(asc_release.WATCH_DISPLAY_TYPES))
        self.assertEqual(self.watch.accepted[0], (416, 496))

    def test_without_a_watch_folder_nothing_is_staged_and_stale_frames_go(self):
        self.write(self.watch.dest, "01-stale.png", size=(416, 496))

        copied = self.stage(dry_run=False, platform="ios")

        self.assertEqual(self.names(self.watch.dest), [])
        self.assertNotIn("01-stale.png", [p.name for p in copied])
        self.assertEqual(self.names(self.ios_dest), ["01-a.png"])

    def test_play_staging_leaves_staged_watch_frames_alone(self):
        self.write(self.play_src, "01-x.png")
        self.write(self.watch.dest, "01-staged.png", size=(416, 496))

        self.stage(dry_run=False, platform="play")

        self.assertEqual(self.names(self.watch.dest), ["01-staged.png"])

    def test_a_frame_that_is_not_watch_shaped_fails_before_anything_is_replaced(self):
        self.write(self.watch.source, "01-phone.png")
        self.write(self.watch.dest, "01-staged.png", size=(416, 496))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.stage(dry_run=False, platform="ios")
        self.assertEqual(self.names(self.watch.dest), ["01-staged.png"])

    def test_upload_without_pillow_refuses_frames_that_store_alpha(self):
        cases = {
            "rgba": png_file(1290, 2796, 6),
            "grey + alpha": png_file(1290, 2796, 4),
            "rgb + tRNS": png_file(1290, 2796, 2, (b"tRNS", b"\x00" * 6)),
        }
        self.write(self.ios_dest, "01-staged.png")
        with mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"}):
            for label, raw in cases.items():
                (self.ios_src / "02-alpha.png").write_bytes(raw)
                stderr = io.StringIO()
                with self.subTest(label), self.assertRaises(SystemExit), redirect_stderr(stderr):
                    self.stage(dry_run=False, platform="ios")
                self.assertIn("02-alpha.png", stderr.getvalue())
                self.assertEqual(self.names(self.ios_dest), ["01-staged.png"])
            # An RGB file is copied as it is.
            (self.ios_src / "02-alpha.png").write_bytes(png_file(1290, 2796, 2))
            self.stage(dry_run=False, platform="ios")
        self.assertEqual(self.names(self.ios_dest), ["01-a.png", "02-alpha.png"])


@unittest.skipIf(
    PILLOW is None and not os.environ.get("STORE_RELEASE_TESTS_NEED_PILLOW"), "needs Pillow (not installed)"
)
class RgbStageTests(SetsFixture):
    """With Pillow, every App Store frame is staged as an RGB PNG without alpha."""

    needs_pillow = True
    PILLOW = PILLOW
    OPAQUE = (10, 20, 30, 255)
    HIDDEN_RED = (255, 0, 0, 0)  # fully transparent, with a colour that must never show
    HALF = (200, 100, 50, 128)

    def setUp(self):
        if self.PILLOW is None:
            self.fail("Pillow is required here (STORE_RELEASE_TESTS_NEED_PILLOW is set)")
        super().setUp()
        self.frame(self.ipad_src, "01-i.png", (2064, 2752))
        self.frame(self.duo_src, "01-d.png", (1398, 2034))
        upload = mock.patch.dict(os.environ, {"STORE_UPLOAD_SCREENSHOTS": "true"})
        upload.start()
        self.addCleanup(upload.stop)

    def frame(self, directory: Path, name: str, size: tuple[int, int], *, icc: bytes | None = None,
              chunks: tuple[tuple[bytes, bytes], ...] = ()) -> Path:
        from PIL import PngImagePlugin

        img = self.PILLOW.new("RGBA", size, self.OPAQUE)
        width, height = size
        # The left quarter is transparent; one translucent pixel top right.
        img.paste(self.HIDDEN_RED, (0, 0, width // 4, height))
        img.putpixel((width - 1, 0), self.HALF)
        info = PngImagePlugin.PngInfo()
        for kind, data in chunks:
            info.add(kind, data)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        img.save(path, pnginfo=info, **({"icc_profile": icc} if icc else {}))
        return path

    def stage_ios(self) -> None:
        self.stage(dry_run=False, platform="ios")

    def rgb(self, path: Path):
        raw = path.read_bytes()
        self.assertEqual(raw[25], 2, f"{path.name} is not an RGB PNG (colour type {raw[25]})")
        self.assertNotIn(b"tRNS", chunk_types(raw))
        img = self.PILLOW.open(path)
        self.assertEqual(img.mode, "RGB")
        return img

    def test_frames_at_an_accepted_size_keep_their_pixels_over_black(self):
        source = self.frame(self.ios_src, "01-a.png", (1290, 2796))
        before = hashlib.sha256(source.read_bytes()).hexdigest()

        self.stage_ios()

        img = self.rgb(self.ios_dest / "01-a.png")
        self.assertEqual(img.size, (1290, 2796))
        self.assertEqual(img.getpixel((1289, 2795)), self.OPAQUE[:3], "opaque pixels are kept as they are")
        self.assertEqual(img.getpixel((0, 0)), (0, 0, 0), "transparent pixels are laid over black")
        # Translucent pixels are blended over black exactly once.
        for got, value in zip(img.getpixel((1289, 0)), self.HALF[:3]):
            self.assertAlmostEqual(got, value * 128 / 255, delta=1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before, "the committed file is untouched")

    def test_frames_that_need_resizing_are_flattened_before_the_resize(self):
        source = self.frame(self.ios_src, "01-a.png", (645, 1398))
        before = source.read_bytes()

        self.stage_ios()

        img = self.rgb(self.ios_dest / "01-a.png")
        self.assertEqual(img.size, (1290, 2796))
        # The hidden colour of the transparent left quarter never leaks into the
        # resized frame (the left half holds only black and the opaque blue-grey).
        from PIL import ImageChops
        red, green, _ = img.crop((0, 0, 645, 2796)).split()
        self.assertLessEqual(ImageChops.subtract(red, green).getextrema()[1], 1)
        self.assertEqual(img.getpixel((0, 1400)), (0, 0, 0))
        self.assertEqual(img.getpixel((1200, 1400)), self.OPAQUE[:3])
        self.assertEqual(source.read_bytes(), before)

    def test_every_app_store_set_is_staged_without_alpha(self):
        self.frame(self.ios_src, "01-a.png", (1290, 2796))
        self.frame(self.ipad_src, "01-i.png", (2064, 2752))
        self.frame(self.duo_src, "01-d.png", (2007, 2853))
        watch = ss.watch_set()
        self.frame(watch.source, "01-w.png", (416, 496))
        self.frame(watch.source, "02-w2x.png", (832, 992))

        self.stage_ios()

        for dest in (self.ios_dest, self.ipad_dest, self.duo_dest, watch.dest):
            for name in self.names(dest):
                with self.subTest(set=dest.name, name=name):
                    self.rgb(dest / name)
        self.assertEqual(self.PILLOW.open(watch.dest / "02-w2x.png").size, (416, 496))

    def test_the_colour_profile_is_kept(self):
        from PIL import ImageCms

        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        gamma = (b"gAMA", (45455).to_bytes(4, "big"))
        chroma = (b"cHRM", bytes(range(32)))
        for size in ((1290, 2796), (645, 1398)):
            source = self.frame(self.ios_src, "01-a.png", size, icc=icc, chunks=(gamma, chroma))
            with self.subTest(size=size):
                self.stage_ios()
                staged = self.ios_dest / "01-a.png"
                self.assertEqual(self.rgb(staged).info.get("icc_profile"), icc)
                kept = {kind: data for kind, data in asc_release.png_chunks(staged.read_bytes())}
                self.assertEqual(kept.get(b"gAMA"), gamma[1])
                self.assertEqual(kept.get(b"cHRM"), chroma[1])
            source.unlink()


if __name__ == "__main__":
    unittest.main()
