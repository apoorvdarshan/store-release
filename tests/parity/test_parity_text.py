"""Parity cases 4-5: listing text, What's New and release notes on identical inputs."""
from __future__ import annotations

import shutil
import sys
import unittest
from unittest import mock

from store_release import release_notes
from store_release.text import listing, whats_new
from support import apps as support_apps
from support.upstream import ParityCase, outcome, strip_usage, tree_files

support_apps.configure_fud_ai_defaults()


def replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


class ListingAndWhatsNewParityTests(ParityCase):
    def mutations(self) -> list[tuple[str, str, str]]:
        """(label, APPSTORE.md, PLAYSTORE.md) variants, the unchanged docs first."""
        ios = (self.app.root / "APPSTORE.md").read_text()
        play = (self.app.root / "PLAYSTORE.md").read_text()
        return [
            ("unchanged", ios, play),
            ("missing heading", replace_once(ios, "## Keywords (100 chars max)", "## Search Words"), play),
            ("unclosed fence", replace_once(ios, "It has two paragraphs.\n```\n", "It has two paragraphs.\n\n"), play),
            ("subtitle cap + 1", replace_once(ios, "Track it with AI", "x" * 31), play),
            ("keywords at the cap", replace_once(ios, "food,tracker,calories", "k" * 100), play),
            ("ios what's new markdown", replace_once(ios, "• Faster logging\n- Fixes", "**Faster** logging"), play),
            ("ios what's new too long", replace_once(ios, "• Faster logging\n- Fixes", "x" * 4001), play),
            ("ios what's new empty", replace_once(ios, "• Faster logging\n- Fixes", ""), play),
            ("play short description cap + 1", ios, replace_once(play, "Track food with AI in seconds.", "s" * 81)),
            ("play empty block", ios, replace_once(play, "<en-US>\n• Faster logging\n</en-US>", "<en-US>\n\n</en-US>")),
            ("play missing en-US", ios, replace_once(play, "<en-US>\n• Faster logging\n</en-US>\n", "")),
            ("play what's new link", ios, replace_once(play, "• Faster logging\n</en-US>",
                                                       "See [notes](https://example.com)\n</en-US>")),
            ("play what's new 501", ios, replace_once(play, "• Faster logging\n</en-US>", "y" * 501 + "\n</en-US>")),
            ("play no what's new fence", ios, replace_once(
                play, "```\n<en-US>\n• Faster logging\n</en-US>\n<de-DE>\n• Schneller\n</de-DE>\n```\n", "None yet.\n")),
        ]

    def run_both(self, upstream_module, package_module, argv: list[str]) -> None:
        out_up, out_pkg = self.tmp / "out-upstream", self.tmp / "out-package"
        with mock.patch.object(sys, "argv", ["script", *argv, "--out", str(out_up)]):
            upstream = outcome(upstream_module.main)
        with mock.patch.object(sys, "argv", ["script", *argv, "--out", str(out_pkg)]):
            package = outcome(package_module.main)
        normalize = lambda result, out: (result[0], result[1], result[2].replace(str(out), "<OUT>"),  # noqa: E731
                                         result[3].replace(str(out), "<OUT>"))
        self.assertEqual(normalize(package, out_pkg), normalize(upstream, out_up))
        self.assertEqual(tree_files(out_pkg), tree_files(out_up))

    def test_every_mutation_on_every_platform(self) -> None:
        for label, ios, play in self.mutations():
            (self.app.root / "APPSTORE.md").write_text(ios)
            (self.app.root / "PLAYSTORE.md").write_text(play)
            for platform in ("ios", "play", "all"):
                with self.subTest(label=label, platform=platform, script="listing"):
                    self.run_both(self.up.prepare_listing_text, listing, ["--platform", platform])
                with self.subTest(label=label, platform=platform, script="whats-new"):
                    self.run_both(self.up.prepare_whats_new, whats_new, ["--platform", platform, "--tag", "ios-v7.2"])
            for folder in ("out-upstream", "out-package"):
                shutil.rmtree(self.tmp / folder, ignore_errors=True)


class ReleaseNotesParityTests(ParityCase):
    TEXTS = {
        "present": "# Notes\n\n## ios-v7.2\n\nShipped.\n\n## android-v7.2\n\nBeta.\n",
        "missing": "# Notes\n\n## ios-v7.1\n\nOld.\n",
        "empty": "# Notes\n\n## ios-v7.2\n\n## android-v7.2\n\nBeta.\n",
        "duplicated": "## ios-v7.2\n\nA.\n\n## ios-v7.2\n\nB.\n",
        "unreleased": "## Unreleased\n\nWIP.\n",
        "no file": None,
    }

    def test_every_case_with_and_without_if_present(self) -> None:
        path = self.app.root / "RELEASE_NOTES.md"
        for label, text in self.TEXTS.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text)
            for tag in ("ios-v7.2", "android-v7.2", "Unreleased"):
                for args in ([tag], ["--if-present", tag]):
                    with self.subTest(label=label, args=args):
                        upstream = outcome(lambda: self.up.release_notes.main(args))
                        package = outcome(lambda: release_notes.main(args))
                        self.assertEqual(package[:3], upstream[:3])
                        # argparse's usage line names the program and the new --check-headings (D15).
                        self.assertEqual(strip_usage(package[3]), strip_usage(upstream[3]))


if __name__ == "__main__":
    unittest.main()
