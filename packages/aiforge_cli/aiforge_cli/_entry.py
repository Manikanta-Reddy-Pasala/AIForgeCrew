"""The script PyInstaller freezes.

A frozen entry script runs as top-level ``__main__`` with no package context,
so a relative import inside it fails with "attempted relative import with no
known parent package". This module exists to be that script with ABSOLUTE
imports; `python -m aiforge_cli` keeps using __main__.py.
"""

from __future__ import annotations

import sys

from aiforge_cli.cli import main

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        # 130 is what a shell reads as "the user stopped it". main() lets the
        # interrupt through on purpose; this is the process boundary.
        sys.exit(130)
