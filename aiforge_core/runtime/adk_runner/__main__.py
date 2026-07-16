"""``python -m aiforge_core.runtime.adk_runner`` entrypoint.

Preserves the original module's ``if __name__ == "__main__": sys.exit(main())``
behaviour now that ``adk_runner`` is a package (``python -m`` runs this file).
"""
from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
