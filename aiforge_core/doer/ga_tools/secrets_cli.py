"""CLI shim for the secrets scanner — invoked by hooks.builtin_hooks
during pre_commit. Exits non-zero when secrets are found so the
hooks dispatcher trips ``block: true``.

Usage:  python -m aiforge_core.doer.ga_tools.secrets_cli <worktree>
"""
from __future__ import annotations

import sys
from . import secrets


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: secrets_cli <worktree>", file=sys.stderr)
        return 2
    worktree = argv[1]
    n, summary = secrets.scan(worktree)
    print(summary)
    return 1 if n > 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
