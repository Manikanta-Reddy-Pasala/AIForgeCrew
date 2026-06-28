"""``aiforge-memory ops`` — operational helpers (backup + log rotation).

Both sub-actions live here because they share the
:mod:`aiforge_memory.ops.backup` module and the same exit-status
convention (0 only when no errors).
"""
from __future__ import annotations

import argparse
import json

from aiforge_memory.ops import backup as ops_backup


def run_backup(args: argparse.Namespace) -> int:
    res = ops_backup.backup_state()
    rotated = ops_backup.rotate_backups(keep=args.keep)
    print(json.dumps({
        "backed_up": res.backed_up,
        "rotated_out": rotated.rotated_out,
        "errors": res.errors + rotated.errors,
    }, indent=2))
    return 0 if not (res.errors or rotated.errors) else 1


def run_rotate_logs(_args: argparse.Namespace) -> int:
    out = ops_backup.rotate_known_logs()
    print(json.dumps({"rotated": out}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ops", help="Operational helpers: backup + log rotation",
    )
    ops_sub = p.add_subparsers(dest="ops_cmd", required=True)

    p_b = ops_sub.add_parser(
        "backup", help="VACUUM INTO snapshot of state.db; rotates oldest",
    )
    p_b.add_argument("--keep", type=int, default=7)
    p_b.set_defaults(func=run_backup)

    p_r = ops_sub.add_parser(
        "rotate-logs", help="Rotate AiForge logs over 10MB",
    )
    p_r.set_defaults(func=run_rotate_logs)


__all__ = ["run_backup", "run_rotate_logs", "register"]
