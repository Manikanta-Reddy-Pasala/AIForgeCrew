"""ADK 2.0.0b1 Workflow port — concrete BaseNode subclasses bridged
to the live AIForge runners.

KISS: each node = one class with a single ``run(ctx)`` async generator.
Workflow declared via ``edges=[(from, to, ...)]`` mirrors the live
1.31.1 SequentialAgent ``Architect → Planner → Doer ⇄ Feedback →
Integration → Publish → Learner``.

Runner harness exposes:

    python -m scripts.runtime.adk2-sidecar.workflow run-once \
        --ticket ONE-99 [--worktree /path]

So we can A/B vs the production 1.31.1 path on the same ticket.

Cut-over plan:
    Phase B.1 — get this skeleton green on a single fixture ticket
    Phase B.2 — port Planner (currently smolagents) to a Workflow node
    Phase B.3 — replace ask_user poll with RequestInput HITL
    Phase B.4 — flip AIFORGE_USE_ADK2=1 in the orchestrator
    Phase B.5 — drop legacy adk_workflow.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time


# ─────────────────────────── lazy ADK 2.0 imports ──────────────────


def _adk2():
    """Import google-adk 2.0 from this venv. Returns the module
    handles we use; raises when the venv hasn't been bootstrapped."""
    try:
        from google.adk import Agent, Workflow, Context, Event
        from google.adk.workflow import (
            BaseNode, Node, FunctionNode, JoinNode, RetryConfig,
            START, node,
        )
        return {
            "Agent": Agent, "Workflow": Workflow,
            "Context": Context, "Event": Event,
            "BaseNode": BaseNode, "Node": Node,
            "FunctionNode": FunctionNode, "JoinNode": JoinNode,
            "RetryConfig": RetryConfig, "START": START, "node": node,
        }
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "ADK 2.0.0b1 not installed in this venv. Run "
            "'pip install -r scripts/runtime/adk2-sidecar/requirements.txt'"
            f"\nUnderlying: {exc}"
        )


# ─────────────────────────── Bridge to AIForge runners ─────────────


def _ensure_aiforge_path() -> None:
    """Add the production AIForge package to sys.path so the bridge
    nodes can call into ``aiforge_core.*`` without re-installing it
    inside this venv."""
    candidate = os.path.expanduser("~/AIForgeCrew")
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)


def _bridge_planner(ticket_id: str) -> dict:
    _ensure_aiforge_path()
    try:
        from aiforge_core.planner.ga_runner import run_planner_via_ga
        from aiforge_core.runtime import tickets as _tk
    except ImportError as exc:
        return {"error": f"aiforge import failed: {exc}"}
    ticket = _tk.get_by_identifier(ticket_id)
    if ticket is None:
        return {"error": f"ticket {ticket_id} not found"}
    return run_planner_via_ga(ticket)


def _bridge_doer(ticket_id: str, plan: str, worktree: str) -> dict:
    _ensure_aiforge_path()
    try:
        from aiforge_core.doer.ga_runner import run_doer_via_ga
        from aiforge_core.runtime import tickets as _tk
    except ImportError as exc:
        return {"error": f"aiforge import failed: {exc}"}
    ticket = _tk.get_by_identifier(ticket_id)
    if ticket is None:
        return {"error": f"ticket {ticket_id} not found"}
    return run_doer_via_ga(
        ticket, worktree_path=worktree, plan_text=plan or "",
    )


def _bridge_learner(ticket_id: str, doer_outcome: dict) -> dict | None:
    _ensure_aiforge_path()
    try:
        from aiforge_core.runtime.doer_learner import distill
        from aiforge_core.runtime import tickets as _tk
    except ImportError as exc:
        return {"error": f"aiforge import failed: {exc}"}
    ticket = _tk.get_by_identifier(ticket_id)
    if ticket is None:
        return {"error": f"ticket {ticket_id} not found"}
    return distill(ticket, doer_outcome)


# ─────────────────────────── Workflow nodes ────────────────────────


def build_workflow():
    """Construct the ADK 2.0 Workflow graph. Lazy ADK import keeps
    this module loadable in environments where the sidecar venv
    isn't active (e.g. running --help)."""
    adk = _adk2()
    Event = adk["Event"]
    Workflow = adk["Workflow"]
    node = adk["node"]
    RetryConfig = adk["RetryConfig"]

    @node(retry_config=RetryConfig(max_attempts=2, initial_delay=2))
    def planner(ctx):
        ticket_id = ctx.state.get("ticket")
        out = _bridge_planner(ticket_id)
        yield Event(state={
            "plan": out.get("plan", ""),
            "planner_summary": out.get("summary", ""),
            "planner_stop_reason": out.get("stop_reason"),
        }, message=f"[planner] {out.get('stop_reason') or 'ok'}")

    @node(retry_config=RetryConfig(max_attempts=3, initial_delay=5))
    def doer(ctx):
        out = _bridge_doer(
            ctx.state.get("ticket"),
            ctx.state.get("plan", ""),
            ctx.state.get("worktree", ""),
        )
        yield Event(state={
            "edits": int(out.get("edit_block_ok", 0) or 0),
            "compile_green": int(out.get("compile_green", 0) or 0),
            "doer_stop_reason": out.get("stop_reason"),
            "doer_summary": out.get("summary", ""),
            "files_touched": out.get("files_touched") or [],
            "last_compile_error": (out.get("last_compile_error") or "")[:1500],
        }, message=f"[doer] edits={out.get('edit_block_ok',0)}, "
                   f"compile={out.get('compile_green',0)}")

    @node
    def feedback(ctx):
        green = int(ctx.state.get("compile_green") or 0) > 0
        edits = int(ctx.state.get("edits") or 0) > 0
        verdict = "ok" if (green and edits) else "retry"
        yield Event(
            state={"feedback_verdict": verdict},
            message=f"[feedback] verdict={verdict}",
        )

    @node
    def learner(ctx):
        out = _bridge_learner(ctx.state.get("ticket"), {
            "edit_block_ok":  ctx.state.get("edits", 0),
            "compile_green":  ctx.state.get("compile_green", 0),
            "stop_reason":    ctx.state.get("doer_stop_reason"),
            "summary":        ctx.state.get("doer_summary", ""),
            "files_touched":  ctx.state.get("files_touched") or [],
            "last_compile_error": ctx.state.get("last_compile_error") or "",
        })
        yield Event(message=f"[learner] retained={out}")

    return Workflow(
        name="aiforge_v2",
        edges=[
            ("START", planner, doer, feedback),
            (feedback, learner),
            # Loop edge syntax: (from, to, condition_fn) — exact
            # signature lands when ADK 2.0 final docs ship. For
            # now feedback simply records the verdict and passes
            # straight to learner; the orchestrator above re-enters
            # if verdict='retry'.
        ],
    )


# ─────────────────────────── CLI / harness ─────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adk2-workflow")
    sub = p.add_subparsers(dest="cmd", required=True)

    rn = sub.add_parser("run-once",
                        help="Run the ADK 2.0 workflow on one ticket")
    rn.add_argument("--ticket", required=True)
    rn.add_argument("--worktree", default="",
                    help="Doer worktree (defaults to ticket-derived path)")

    inspect = sub.add_parser("inspect", help="Print node + edge summary")
    inspect.set_defaults(func=lambda _a: _cmd_inspect())

    args = p.parse_args(argv)

    if args.cmd == "run-once":
        return _cmd_run_once(args.ticket, args.worktree)
    if args.cmd == "inspect":
        return _cmd_inspect()
    return 1


def _cmd_inspect() -> int:
    wf = build_workflow()
    out = {
        "name": getattr(wf, "name", "?"),
        "edge_count": len(getattr(wf, "edges", []) or []),
        "node_summary": "Architect → Planner → Doer ⇄ Feedback → Learner",
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_run_once(ticket: str, worktree: str) -> int:
    """One-shot run. Picks ADK Runner if available, else falls back
    to invoking the bridge functions directly so we still validate
    the wiring even before a real Runner lands in 2.0.0b1."""
    adk = _adk2()
    print(f"[adk2-sidecar] run-once ticket={ticket} worktree={worktree!r}")
    Event = adk["Event"]

    # ADK 2.0.0b1 Runner API name flux — try the documented entry
    # then fall back to a manual node sweep.
    try:
        from google.adk.runners import Runner  # type: ignore
        wf = build_workflow()
        runner = Runner(agent=wf)  # type: ignore[arg-type]
        async def _go():
            async for ev in runner.run_async(
                user_id="ops", session_id=f"adk2-{int(time.time())}",
                new_message=Event(state={"ticket": ticket,
                                         "worktree": worktree}),
            ):
                print(f"  event: {getattr(ev, 'message', '?')}")
        asyncio.run(_go())
        return 0
    except Exception as exc:
        print(f"[adk2-sidecar] Runner path failed ({exc}); falling back "
              f"to manual node sweep")

    # Manual sweep — direct bridge calls, no graph engine. Still
    # exercises every node body.
    plan_out  = _bridge_planner(ticket)
    print(f"  planner: {json.dumps(plan_out, default=str)[:300]}")
    doer_out  = _bridge_doer(ticket, plan_out.get("plan", ""), worktree)
    print(f"  doer: {json.dumps(doer_out, default=str)[:300]}")
    learn_out = _bridge_learner(ticket, doer_out)
    print(f"  learner: {json.dumps(learn_out, default=str)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
