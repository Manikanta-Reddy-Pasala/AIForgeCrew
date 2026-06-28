"""``aiforge-memory`` operator CLI — argparse wiring only.

Each subcommand lives in its own module under :mod:`.commands` so a
behavioural change touches one file. This module's job is to compose
the argparse tree and dispatch to ``args.func(args)``.

Adding a new command:
  1. Drop ``aiforge_memory/api/commands/<name>.py`` exporting
     ``run(args)`` and ``register(sub_parsers)``.
  2. Append the module to ``commands.COMMAND_MODULES``.
"""
from __future__ import annotations

import argparse
import sys

from .commands import COMMAND_MODULES


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    for mod in COMMAND_MODULES:
        mod.register(sub)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
