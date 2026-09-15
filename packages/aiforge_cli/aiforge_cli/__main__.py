"""`python -m aiforge_cli`, and the entry point the frozen binary runs."""

from __future__ import annotations

import sys


def main() -> int:
    from .cli import main as run
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
