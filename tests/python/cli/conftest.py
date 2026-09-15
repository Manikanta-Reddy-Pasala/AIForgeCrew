"""Make the CLI package importable without installing it.

`aiforge_cli` is a separate wheel that is frozen into the shipped binaries, so
it is deliberately NOT a dependency of the app — nothing in aiforge_core
imports it. Adding its source root here is what lets the suite test it in place
(and keeps uv.lock untouched, which run.sh's lockfile-only install rule cares
about).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "packages" / "aiforge_cli"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
