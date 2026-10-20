#!/usr/bin/env python3
"""store-release launcher.

    python3 -I -u .store-release/run.py [--app-root DIR] [--config PATH] <command> [args...]

-I (isolated mode) keeps the script's directory and the working directory off
sys.path, so this file puts the tooling checkout there itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store_release.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
