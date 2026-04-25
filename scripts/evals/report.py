"""Aggregate per-run JSON metrics across tracks/fixtures into a table.

Reads everything under evals/results/{track}/{fixture}/*.json and emits
a markdown summary suitable for a PR / Telegram update.

Usage:
    python scripts/evals/report.py
    python scripts/evals/report.py --track Z --track A   # subset
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, median
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"


def _load_runs(track: str | None = None) -> list[dict]:
    out: list[dict] = []
    for trk_dir in sorted(RESULTS_DIR.iterdir()) if RESULTS_DIR.exists() else []:
        if not trk_dir.is_dir():
            continue
        if track and trk_dir.name != track:
            continue
        for fix_dir in sorted(trk_dir.iterdir()):
            if not fix_dir.is_dir():
                continue
            for fp in sorted(fix_dir.glob("*.json")):
                try:
                    out.append(json.loads(fp.read_text()))
                except Exception:
                    continue
    return out


def _agg(values: Iterable[float]) -> str:
    vs = [v for v in values if v is not None]
    if not vs:
        return "—"
    if len(vs) == 1:
        return f"{vs[0]:.0f}"
    return f"{mean(vs):.0f} ({min(vs):.0f}-{max(vs):.0f})"


def _pct(values: Iterable[bool]) -> str:
    vs = list(values)
    if not vs:
        return "—"
    return f"{int(sum(1 for v in vs if v) * 100 / len(vs))}% ({sum(1 for v in vs if v)}/{len(vs)})"


def _summary(runs: list[dict]) -> str:
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in runs:
        m = r.get("metrics") or {}
        key = (r.get("track", "?"), r.get("fixture_id", "?"))
        by_key.setdefault(key, []).append(m)
    lines = [
        "# Eval results summary",
        "",
        "| Track | Fixture | n | compile% | reached% | tokens | steps | wall(s) | grep | graph_rag | explorer |",
        "|-------|---------|---|----------|----------|--------|-------|---------|------|-----------|----------|",
    ]
    for (track, fix), mlist in sorted(by_key.items()):
        n = len(mlist)
        comp = _pct(m.get("compile_pass", False) for m in mlist)
        reached = _pct(m.get("final_answer_reached", False) for m in mlist)
        toks = _agg(m.get("total_tokens", 0) for m in mlist)
        steps = _agg(m.get("llm_call_count") or m.get("steps") or 0 for m in mlist)
        wall = _agg(m.get("wall_clock_s", 0) for m in mlist)
        grep = _agg(m.get("grep_calls", 0) for m in mlist)
        graph = _agg(m.get("graph_rag_calls", 0) for m in mlist)
        expl = _agg(m.get("explorer_calls", 0) for m in mlist)
        lines.append(
            f"| {track} | {fix} | {n} | {comp} | {reached} | {toks} | {steps} | {wall} | {grep} | {graph} | {expl} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", action="append", default=None,
                    help="Only include this track (repeatable)")
    args = ap.parse_args()
    if args.track:
        runs = []
        for t in args.track:
            runs.extend(_load_runs(t))
    else:
        runs = _load_runs()
    print(_summary(runs))
    print()
    print(f"Total runs loaded: {len(runs)}")


if __name__ == "__main__":
    main()
