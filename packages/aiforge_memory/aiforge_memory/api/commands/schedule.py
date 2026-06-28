"""``aiforge-memory schedule`` — periodic git fetch/pull + delta ingest.

Six sub-actions kept in one file because they're all thin wrappers
over :mod:`aiforge_memory.features.scheduler.runner`. Splitting each
into its own file would produce 6 ~10-line modules without any reuse
benefit — the trade-off swings the other way for this group.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from aiforge_memory.features.scheduler import runner as scheduler


def run_add(args: argparse.Namespace) -> int:
    rs = scheduler.RepoSchedule(
        name=args.repo,
        path=str(Path(args.path or os.getcwd()).resolve()),
        interval_seconds=args.interval,
        pull=not args.no_pull,
        skip_services=args.skip_services,
        skip_summaries=args.skip_summaries,
        skip_chunks=args.skip_chunks,
        use_lsp=args.use_lsp,
        timeout_seconds=args.timeout,
    )
    scheduler.add_repo(rs)
    print(json.dumps({"added": rs.__dict__,
                      "config": str(scheduler.CONFIG_PATH)}, indent=2))
    return 0


def run_remove(args: argparse.Namespace) -> int:
    ok = scheduler.remove_repo(args.repo)
    print(json.dumps({"removed": ok, "repo": args.repo}))
    return 0 if ok else 1


def run_list(_args: argparse.Namespace) -> int:
    cfg = scheduler.SchedulerConfig.load()
    print(json.dumps({
        "config": str(scheduler.CONFIG_PATH),
        "repos": [r.__dict__ for r in cfg.repos],
    }, indent=2))
    return 0


def run_run(args: argparse.Namespace) -> int:
    scheduler.run_loop(once=args.once)
    return 0


def run_daemon(_args: argparse.Namespace) -> int:
    pid = scheduler.daemonize()
    print(json.dumps({"daemon_pid": pid,
                      "log": str(scheduler.LOG_PATH)}, indent=2))
    return 0 if pid > 0 else 1


def run_stop(_args: argparse.Namespace) -> int:
    ok = scheduler.stop_daemon()
    print(json.dumps({"stopped": ok}))
    return 0 if ok else 1


def run_status(_args: argparse.Namespace) -> int:
    print(json.dumps(scheduler.daemon_status(), indent=2, default=str))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "schedule",
        help="Periodic git fetch/pull + delta ingest daemon",
    )
    sc_sub = p.add_subparsers(dest="schedule_cmd", required=True)

    sc_add = sc_sub.add_parser("add", help="Add a repo to the schedule")
    sc_add.add_argument("repo")
    sc_add.add_argument("--path", help="Repo dir; defaults to CWD")
    sc_add.add_argument("--interval", type=int, default=600,
                        help="poll interval in seconds (default 600)")
    sc_add.add_argument("--no-pull", action="store_true",
                        help="fetch only — do not run git pull --ff-only")
    sc_add.add_argument("--skip-services", action="store_true",
                        help="skip Stage 3 LLM service extraction "
                             "(useful for doc/note dirs with no services)")
    sc_add.add_argument("--skip-summaries", action="store_true")
    sc_add.add_argument("--skip-chunks", action="store_true")
    sc_add.add_argument("--use-lsp", action="store_true",
                        help="layer LSP-confirmed CALLS on top of tree-sitter")
    sc_add.add_argument("--timeout", type=int, default=1800,
                        help="per-tick wall ceiling in seconds (default 1800)")
    sc_add.set_defaults(func=run_add)

    sc_rm = sc_sub.add_parser("remove",
                              help="Remove a repo from the schedule")
    sc_rm.add_argument("repo")
    sc_rm.set_defaults(func=run_remove)

    sc_ls = sc_sub.add_parser("list", help="List scheduled repos")
    sc_ls.set_defaults(func=run_list)

    sc_run = sc_sub.add_parser("run",
                               help="Run loop in foreground (Ctrl-C to stop)")
    sc_run.add_argument("--once", action="store_true",
                        help="single tick over each repo, then exit")
    sc_run.set_defaults(func=run_run)

    sc_dm = sc_sub.add_parser("daemon",
                              help="Fork into background (POSIX)")
    sc_dm.set_defaults(func=run_daemon)

    sc_st = sc_sub.add_parser("stop",
                              help="Stop the running daemon (SIGTERM)")
    sc_st.set_defaults(func=run_stop)

    sc_status = sc_sub.add_parser(
        "status", help="JSON: pid + per-repo last_run / next_run",
    )
    sc_status.set_defaults(func=run_status)


__all__ = [
    "run_add", "run_remove", "run_list", "run_run",
    "run_daemon", "run_stop", "run_status", "register",
]
