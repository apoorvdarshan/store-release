"""`python3 -m store_release`: the installed store-release command, where its script is not on PATH."""
import sys

from store_release.cli import MODULE, main

if __name__ == "__main__":
    sys.exit(main(prog=MODULE))
