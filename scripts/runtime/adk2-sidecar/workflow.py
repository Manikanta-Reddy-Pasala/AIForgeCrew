"""ADK 2.0.0b1 Workflow port — skeleton for the AIForge sequence.

KISS: same logical graph as the live 1.31.1 ``adk_workflow.py``
(``Architect → Planner → Doer ⇄ Feedback → Integration → Publish →
Learner``) but expressed as a declarative ``Workflow(edges=...)``.

Each ``@node`` is a thin shim around the existing AIForge runners
so we can A/B without rewriting the agent logic itself. Once
parity is confirmed, we rip the 1.31.1 SequentialAgent layer.

Run::

    . .venv-adk2/bin/activate
    python -m scripts.runtime.adk2-sidecar.workflow run-once --ticket ONE-99
"""
from __future__ import annotations

import argparse
import sys


# ─── nodes ────────────────────────────────────────────────────────


def _planner_node(ctx):
    """Bridge to existing planner runner."""
    from google.adk import Event
    # Lazy import — keeps file importable even when AIForge package
    # isn't on path during smoke.
    try:
        from aiforge_core.planner.ga_runner import run_planner_via_ga
    except ImportError as exc:
        yield Event(message=f"[adk2.planner] aiforge import failed: {exc}")
        return
    ticket = ctx.state.get("ticket")
    summary = run_planner_via_ga(ticket)
    yield Event(state={"plan": summary.get("plan", "")},
                message=f"[adk2.planner] {summary.get('stop_reason')}")


def _doer_node(ctx):
    from google.adk import Event
    try:
        from aiforge_core.doer.ga_runner import run_doer_via_ga
    except ImportError as exc:
        yield Event(message=f"[adk2.doer] aiforge import failed: {exc}")
        return
    ticket = ctx.state.get("ticket")
    worktree = ctx.state.get("worktree")
    plan = ctx.state.get("plan", "")
    out = run_doer_via_ga(ticket, worktree_path=worktree, plan_text=plan)
    yield Event(state={
        "edits": out.get("edit_block_ok", 0),
        "compile_green": out.get("compile_green", 0),
        "stop_reason": out.get("stop_reason"),
    }, message=f"[adk2.doer] {out.get('stop_reason')}")


def _feedback_node(ctx):
    from google.adk import Event
    if int(ctx.state.get("compile_green") or 0):
        yield Event(message="[adk2.feedback] green → integration", state={"verdict": "ok"})
    else:
        yield Event(message="[adk2.feedback] red → loop back", state={"verdict": "retry"})


def _learner_node(ctx):
    from google.adk import Event
    try:
        from aiforge_core.runtime.doer_learner import distill
        ticket = ctx.state.get("ticket")
        result = distill(ticket, ctx.state)
    except Exception as exc:
        yield Event(message=f"[adk2.learner] {exc}")
        return
    yield Event(message=f"[adk2.learner] retained={result}")


# ─── workflow ─────────────────────────────────────────────────────


def build_workflow():
    """Construct the Workflow. Importable so an external runner
    (Tekton, dev REPL, A/B harness) can swap nodes by env flag."""
    try:
        from google.adk import Workflow
        from google.adk.workflow import node
    except ImportError as exc:
        raise SystemExit(
            "ADK 2.0.0b1 not installed in this venv. Run "
            "'pip install -r scripts/runtime/adk2-sidecar/requirements.txt'"
            f"\nUnderlying: {exc}"
        )

    planner  = node(_planner_node, name="planner")
    doer     = node(_doer_node,    name="doer")
    feedback = node(_feedback_node, name="feedback")
    learner  = node(_learner_node,  name="learner")

    return Workflow(
        name="aiforge_v2",
        edges=[
            ("START", planner, doer, feedback),
            (feedback, learner),
            # Loop edge: retry verdict → re-enter doer
            # (full edge syntax depends on ADK 2.0 final API).
        ],
    )


# ─── CLI ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adk2-workflow")
    sub = p.add_subparsers(dest="cmd", required=True)
    rn = sub.add_parser("run-once")
    rn.add_argument("--ticket", required=True)
    args = p.parse_args(argv)

    if args.cmd == "run-once":
        wf = build_workflow()
        # ADK 2.0 invocation API will land here once we wire a Runner.
        # Skeleton — real ctx.invoke(...) goes here.
        print(f"[adk2-sidecar] would run {wf.name} on ticket={args.ticket}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
