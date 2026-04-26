#!/usr/bin/env python3
"""Smolagents Planner vs GenericAgent Planner A/B harness.

KISS: runs the same fixture ticket twice — once with
``AIFORGE_PLANNER_BACKEND=smolagents`` (current default) and once
with ``AIFORGE_PLANNER_BACKEND=genericagent`` (swap candidate).
Reports per-side pass/wall/tokens + verdict, persists the verdict
as a T2 fact for future operators.

Wall-clock timer wraps each ``run_planner_via_*`` call. Token
counts pulled from the Doer's `tokens.snapshot_for_ticket` helper
since GA + smolagents both feed it.

Usage:
    python scripts/evals/planner_ab.py --ticket ONE-99
    python scripts/evals/planner_ab.py --ticket ONE-99 --no-retain
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="planner_ab")
    p.add_argument("--ticket", required=True)
    p.add_argument("--no-retain", action="store_true",
                   help="Skip persisting the verdict to memory")
    args = p.parse_args(argv)

    rows: dict[str, dict] = {}
    for backend in ("smolagents", "genericagent"):
        rows[backend] = _run_once(args.ticket, backend)

    verdict = _verdict(rows)
    print(json.dumps({
        "ticket": args.ticket,
        "rows": rows,
        "verdict": verdict,
    }, indent=2, default=str))

    if not args.no_retain:
        _persist(args.ticket, rows, verdict)
    return 0 if verdict["winner"] != "tie" else 0


def _run_once(ticket_id: str, backend: str) -> dict:
    """Force the env flag and run the Planner. Returns a row dict."""
    prev = os.environ.get("AIFORGE_PLANNER_BACKEND")
    os.environ["AIFORGE_PLANNER_BACKEND"] = backend

    out: dict = {"backend": backend}
    t0 = time.time()
    try:
        from aiforge_core.runtime import tickets as _tk
        ticket = _tk.get_by_identifier(ticket_id)
        if ticket is None:
            return {**out, "error": f"ticket {ticket_id} not found"}
        if backend == "smolagents":
            from aiforge_core.planner.runner import run_planner as _run
        else:
            from aiforge_core.planner.ga_runner import run_planner_via_ga as _run
        summary = _run(ticket, log=None) or {}
        out.update({
            "stop_reason": summary.get("stop_reason"),
            "turns": summary.get("turns"),
            "summary_chars": len(summary.get("summary") or ""),
        })
    except Exception as exc:
        out["error"] = str(exc)[:300]
    finally:
        out["wall_s"] = round(time.time() - t0, 2)
        if prev is None:
            os.environ.pop("AIFORGE_PLANNER_BACKEND", None)
        else:
            os.environ["AIFORGE_PLANNER_BACKEND"] = prev

    out["tokens"] = _tokens_for(ticket_id)
    return out


def _tokens_for(ticket_id: str) -> dict:
    try:
        from aiforge_core.doer.ga_tools import tokens as _tk
        return _tk.snapshot_for_ticket(ticket_id) or {}
    except Exception:
        return {}


def _verdict(rows: dict[str, dict]) -> dict:
    """Pick winner. ok-stop wins over error; faster wins on tie."""
    s = rows.get("smolagents", {})
    g = rows.get("genericagent", {})
    s_ok = "error" not in s and (s.get("stop_reason") not in (None, "exception"))
    g_ok = "error" not in g and (g.get("stop_reason") not in (None, "exception"))

    if s_ok and not g_ok:
        return {"winner": "smolagents", "reason": "ga errored"}
    if g_ok and not s_ok:
        return {"winner": "genericagent", "reason": "smolagents errored"}
    if not s_ok and not g_ok:
        return {"winner": "tie", "reason": "both errored"}

    s_wall = float(s.get("wall_s") or 1e9)
    g_wall = float(g.get("wall_s") or 1e9)
    if s_wall < g_wall * 0.85:
        return {"winner": "smolagents",
                "reason": f"faster ({s_wall}s vs {g_wall}s)"}
    if g_wall < s_wall * 0.85:
        return {"winner": "genericagent",
                "reason": f"faster ({g_wall}s vs {s_wall}s)"}
    return {"winner": "tie", "reason": "wall within 15%"}


def _persist(ticket: str, rows: dict, verdict: dict) -> None:
    try:
        from aiforge_core.runtime.memory import Memory
    except Exception:
        return
    text = (
        f"Planner A/B · ticket {ticket}\n"
        f"smolagents: {rows.get('smolagents')}\n"
        f"genericagent: {rows.get('genericagent')}\n"
        f"verdict: {verdict}"
    )
    try:
        Memory().retain_fact(
            text=text, tier="t2",
            wing="patterns/planner-ab",
            kind="ab_eval",
            source="planner_ab.py",
            metadata={"ticket": ticket, "verdict": verdict},
        )
    except Exception as exc:
        print(f"[planner_ab] persist failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
