"""The vendored fud-ai scripts are verbatim: every file matches MANIFEST.sha256, and nothing else is there."""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

UPSTREAM = Path(__file__).resolve().parent / "upstream_fud_ai_a99591908"


class ManifestTests(unittest.TestCase):
    def test_every_vendored_file_matches_its_hash(self) -> None:
        listed = {}
        for line in (UPSTREAM / "MANIFEST.sha256").read_text().splitlines():
            digest, name = line.split(maxsplit=1)
            listed[name] = digest
        present = sorted(path.name for path in UPSTREAM.iterdir() if path.name != "MANIFEST.sha256")
        self.assertEqual(sorted(listed), present)
        self.assertEqual(len(listed), 12)
        for name, digest in listed.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((UPSTREAM / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
