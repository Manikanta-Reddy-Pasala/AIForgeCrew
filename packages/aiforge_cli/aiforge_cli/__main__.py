"""`python -m aiforge_cli`, and the entry point the frozen binary runs."""

from __future__ import annotations

import sys


def main() -> int:
    return _run(sys.argv[1:])


def _run(argv: list[str]) -> int:
    """main(), with the one thing a library function must not swallow.

    An interrupt has to reach the process boundary — code 130 is what a shell
    reads as "the user stopped it" — but catching it inside main() would hide
    it from every caller, tests included.
    """
    from .cli import main
    try:
        return main(argv)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
