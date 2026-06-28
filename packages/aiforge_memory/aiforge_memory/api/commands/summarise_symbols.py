"""``aiforge-memory summarise-symbols`` — LLM-summarise non-trivial symbols.

Walks the repo's symbols, LLM-summarises non-trivial ones, writes onto
``Symbol_v2.summary``. No re-walk — uses on-disk source via tree-sitter
so this is safe to run after ingest. Idempotent: skips symbols that
already have a summary unless ``--redo-existing`` is set.
"""
from __future__ import annotations

import argparse
import json

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    from aiforge_memory.features.scheduler import runner as sched
    from aiforge_memory.features.symbol import extract as treesitter_walk
    from aiforge_memory.features.symbol import store_summary as symbol_summary_writer
    from aiforge_memory.features.symbol import summarise as symbol_summary

    path = args.path
    if not path:
        for r in sched.SchedulerConfig.load().repos:
            if r.name == args.repo:
                path = r.path
                break
    if not path:
        drv = driver()
        try:
            with drv.session() as s:
                rec = s.run(
                    "MATCH (r:Repo {name:$n}) "
                    "RETURN coalesce(r.path, r.local_path, '') AS p",
                    n=args.repo,
                ).single()
                if rec and rec["p"]:
                    path = rec["p"]
        finally:
            drv.close()
    if not path:
        print(json.dumps({
            "error": "no path",
            "hint": "pass --path or register the repo in the scheduler",
        }))
        return 1

    walked = treesitter_walk.walk_repo(path, repo=args.repo)
    total_walked_syms = sum(len(w.symbols or []) for w in walked)
    print(json.dumps({
        "stage": "walk", "files": len(walked),
        "symbols": total_walked_syms,
    }), flush=True)

    drv = driver()
    # Idempotent resume: drop symbols already summarised so a restart
    # picks up where the previous run stopped instead of redoing work.
    already: set[str] = set()
    if not getattr(args, "redo_existing", False):
        with drv.session() as s:
            for r in s.run(
                "MATCH (sym:Symbol_v2 {repo:$n}) "
                "WHERE sym.summary IS NOT NULL "
                "RETURN sym.fqname AS f", n=args.repo,
            ):
                already.add(r["f"])
        if already:
            for wf in walked:
                wf.symbols = [
                    sym for sym in (wf.symbols or [])
                    if sym.fqname not in already
                ]
            print(json.dumps({
                "stage": "resume",
                "already_summarised": len(already),
                "remaining_walk_symbols":
                    sum(len(w.symbols or []) for w in walked),
            }), flush=True)
    counts = {"written": 0, "trivial": 0, "skipped": 0,
              "missing": 0, "llm_error": 0}
    try:
        def _on_each(ss, idx, total):
            # Stream-write each result so progress shows up in Neo4j +
            # log without waiting for the whole batch (PCB ~9k symbols
            # = hours).
            if not ss.skipped_reason and ss.summary:
                w = symbol_summary_writer.write_symbol_summaries(
                    drv, repo=args.repo, summaries=[ss],
                )
                for k, v in w.items():
                    counts[k] = counts.get(k, 0) + v
            else:
                if ss.skipped_reason == "trivial":
                    counts["trivial"] += 1
                elif ss.skipped_reason == "llm_error":
                    counts["llm_error"] += 1
                elif ss.skipped_reason:
                    counts["skipped"] += 1
            # Emit progress every 25 results so logs don't drown.
            if idx == 1 or idx % 25 == 0 or idx == total:
                print(json.dumps({
                    "stage": "progress",
                    "idx": idx, "total": total,
                    **counts,
                }), flush=True)

        try:
            summaries = symbol_summary.summarise_symbols(
                walked, repo=args.repo, repo_root=path,
                limit=args.limit, min_lines=args.min_lines,
                on_each=_on_each,
            )
        except symbol_summary.SymbolSummaryAborted as exc:
            print(json.dumps({
                "stage": "aborted",
                "reason": str(exc),
                "hint": "restart mlx-lm on MS, then rerun "
                        "(already-written summaries are kept).",
                **counts,
            }, indent=2))
            return 2
    finally:
        drv.close()
    print(json.dumps({
        "stage": "done",
        "candidates": len(summaries),
        **counts,
    }, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "summarise-symbols",
        help="LLM-summarise non-trivial methods/functions for a repo",
    )
    p.add_argument("repo")
    p.add_argument("--path", help="Repo dir; defaults to scheduler entry")
    p.add_argument("--min-lines", type=int, default=8,
                   help="Skip symbols shorter than this many lines (default 8)")
    p.add_argument("--limit", type=int, default=None,
                   help="Hard cap on LLM calls (largest bodies first)")
    p.add_argument("--redo-existing", action="store_true",
                   help="Re-summarise symbols that already have a "
                        "Symbol_v2.summary (default: skip them)")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
