"""GraphPipeline — a custom ADK ``BaseAgent`` that routes the v6 pipeline
as a deterministic graph instead of a flat ``SequentialAgent``.

ADK ships three workflow agents (Sequential / Parallel / Loop) but no
graph type — conditional, data-driven routing is expressed by
subclassing ``BaseAgent`` and driving the children from
``_run_async_impl``. This class is that graph:

    triage_verdict.complexity == "trivial"
        └─► doer_loop ─► validator                      (fast path)

    otherwise
        enhancer ─► context_gather(∥) ─┐
                                       ▼
            ┌──────────────────────────────────────────┐
            │ planner ─► verifier(∥) ─► doer_loop ─► validator │  ◄─┐
            └──────────────────────────────────────────┘     │
                                       │                       │
              validator == request_changes AND replans<max ───┘
                                       │ (else)
                                       ▼
                                    learner

The replan edge sends a failed run back to the Planner ONCE (default),
stashing a ``replan_note`` so the next plan goes smaller — blindly
re-running the same Doer rarely helps (A2 finding). ``context_gather``
and ``verifier`` are ``ParallelAgent`` fan-outs built in
:mod:`parallel_stages`; everything else is an ``LlmAgent`` /
``LoopAgent``. Children are also registered in ``sub_agents`` so ADK
tracks the parent/branch tree correctly.
"""
from __future__ import annotations

import json
from typing import Any

from google.adk.agents import BaseAgent


def _read_complexity(state: Any) -> str:
    """Pull the triage complexity verdict from state if present.

    Accepts ``state['complexity']`` (pre-seeded) or the triage agent's
    ``triage_verdict`` JSON. Defaults to ``"moderate"`` (full path) when
    absent — the fast path only fires on an explicit ``trivial`` signal.
    """
    try:
        c = state.get("complexity")
        if isinstance(c, str) and c:
            return c.lower()
        raw = state.get("triage_verdict")
        if isinstance(raw, dict):
            return str(raw.get("complexity", "moderate")).lower()
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
            if isinstance(obj, dict):
                return str(obj.get("complexity", "moderate")).lower()
    except Exception:
        pass
    return "moderate"


def _validator_failed(state: Any) -> bool:
    """True when the Validator asked for changes (the replan trigger)."""
    try:
        raw = state.get("validator_verdict")
        verdict = None
        if isinstance(raw, dict):
            verdict = raw.get("verdict")
        elif isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            try:
                obj = json.loads(text)
                verdict = obj.get("verdict") if isinstance(obj, dict) else None
            except Exception:
                verdict = text.lower()
        if verdict is None:
            return False
        return str(verdict).lower() in ("request_changes", "reject", "fail")
    except Exception:
        return False


class GraphPipeline(BaseAgent):
    """Deterministic graph router over the v6 archetypes."""

    enhancer: BaseAgent
    context_gather: BaseAgent
    planner: BaseAgent
    verifier: BaseAgent
    doer_loop: BaseAgent
    learner: BaseAgent
    validator: BaseAgent
    max_replans: int = 1

    async def _run_async_impl(self, ctx):  # type: ignore[no-untyped-def]
        state = ctx.session.state
        complexity = _read_complexity(state)

        # ---- fast path: trivial ticket skips planning/context/verify ----
        if complexity == "trivial":
            yield self._route_event(complexity, ["doer_loop", "validator"])
            async for ev in self.doer_loop.run_async(ctx):
                yield ev
            async for ev in self.validator.run_async(ctx):
                yield ev
            return

        # ---- full path -------------------------------------------------
        yield self._route_event(
            complexity,
            ["enhancer", "context_gather", "planner", "verifier",
             "doer_loop", "validator", "learner"],
        )
        async for ev in self.enhancer.run_async(ctx):
            yield ev
        async for ev in self.context_gather.run_async(ctx):
            yield ev

        replans = 0
        while True:
            async for ev in self.planner.run_async(ctx):
                yield ev
            async for ev in self.verifier.run_async(ctx):
                yield ev
            async for ev in self.doer_loop.run_async(ctx):
                yield ev
            async for ev in self.validator.run_async(ctx):
                yield ev

            if replans >= self.max_replans or not _validator_failed(state):
                break
            replans += 1
            # Persist + signal the replan. The state_delta reaches the next
            # Planner pass (same run) AND survives to the final session
            # state the runner inspects.
            yield self._replan_event(replans)

        # Learner runs once, after the run settles — persists facts on the
        # final state via its after_agent_callback.
        async for ev in self.learner.run_async(ctx):
            yield ev

    # -- helpers ---------------------------------------------------------
    def _route_event(self, complexity: str, path: list[str]):
        """Build a state-delta event recording the chosen route. Yielding
        it (vs raw state mutation) is what persists it past the run."""
        route = {"complexity": complexity, "path": path}
        try:
            from .tools._trace import emit
            emit(":GraphRoute", route)
        except Exception:
            pass
        return self._state_event({"graph_route": route})

    def _replan_event(self, replans: int):
        note = (
            f"Validator requested changes (replan {replans}). The prior "
            "plan did not land cleanly — re-plan SMALLER: split the "
            "failing subticket, tighten scope, add the missing test."
        )
        try:
            from .tools._trace import emit
            emit(":Replan", {"replan": replans, "note": note})
        except Exception:
            pass
        return self._state_event(
            {"replan_note": note, "replan_count": replans})

    def _state_event(self, delta: dict):
        from google.adk.events import Event, EventActions
        return Event(
            author=self.name,
            actions=EventActions(state_delta=delta),
        )


__all__ = ["GraphPipeline", "_read_complexity", "_validator_failed"]
