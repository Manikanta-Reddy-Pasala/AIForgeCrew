"""Recovery engine — turns detector matches into orchestrator actions.

Closes the gap between :mod:`failure_taxonomy` (what went wrong),
:mod:`detectors` (how we noticed), and :mod:`recovery` (the action enum).
Before this module the Action enum was dead code: matches were
recorded but never consulted.

Usage from the orchestrator (run_ticket.py):

    eng = RecoveryEngine(log=log, breakers=breakers, ticket_id=ticket_id)
    match = ft.match(model_output) or hallucinated_import_detector.check(...)
    if match:
        decision = eng.handle(match, stage="doer", attempt=attempt)
        if decision.action is Action.REPLAN:
            ...
        elif decision.action is Action.SPLIT_TICKET:
            ...

The engine never raises — it returns a :class:`Decision` with the action
enum, a human-readable rationale, and any kwargs the caller should feed
back into the next stage (e.g. ``unresolved_refs`` for REPLAN).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiforge_core.aiforge_agents.runtime import failure_taxonomy as ft
from aiforge_core.aiforge_agents.runtime import circuit_breakers as cb_mod
from aiforge_core.aiforge_agents.runtime.recovery import Action, decide
from aiforge_core.runtime.logging_setup import emit


@dataclass
class Decision:
    """Outcome of one recovery cycle."""
    action: Action
    rationale: str
    mode_id: str = ""
    kwargs: dict[str, Any] = field(default_factory=dict)
    halt: bool = False


class RecoveryEngine:
    """Per-ticket recovery state machine.

    Tracks how many times each F-mode has fired so repeat offenders
    escalate from local retry → REPLAN → SPLIT → ESCALATE_HUMAN. The
    orchestrator does not need to remember anything between calls; the
    engine carries it.
    """

    # After this many repeats of the same mode the engine forces an
    # ESCALATE_HUMAN regardless of the policy table — prevents infinite
    # retry loops on a poorly-handled F-code.
    _REPEAT_ESCALATE_AT: int = 3

    def __init__(self, *, log=None,
                 breakers: cb_mod.CircuitBreakers | None = None,
                 ticket_id: str = "") -> None:
        self.log = log
        self.breakers = breakers
        self.ticket_id = ticket_id
        self._counts: dict[str, int] = {}
        self.history: list[Decision] = []

    def handle(self, match: ft.FailureMatch, *, stage: str = "",
               attempt: int = 0,
               extra_kwargs: dict[str, Any] | None = None) -> Decision:
        """Map ``match`` to a :class:`Decision`. Records to log + breakers
        + history. Always returns; never raises."""
        mode_id = match.mode.id
        self._counts[mode_id] = self._counts.get(mode_id, 0) + 1
        n = self._counts[mode_id]
        action = decide(mode_id)

        # Repeat-escalation guard
        if n >= self._REPEAT_ESCALATE_AT:
            action = Action.ESCALATE_HUMAN

        # Map action → kwargs the orchestrator can consume directly.
        kwargs: dict[str, Any] = {}
        if action is Action.BLOCK_AND_RETRY:
            kwargs["block_reason"] = match.mode.name
            kwargs["evidence"] = match.evidence
        elif action is Action.REPLAN:
            kwargs["unresolved_refs"] = [
                {"target": match.evidence,
                 "action": "review",
                 "reason": match.mode.name,
                 "mode_id": mode_id}
            ]
        elif action is Action.REPLAN_SMALLER:
            # F-009 token budget — caller should reduce scope/context.
            kwargs["unresolved_refs"] = [{
                "target": "(token budget)",
                "action": "shrink_scope",
                "reason": match.mode.name,
                "mode_id": mode_id,
                "evidence": match.evidence,
            }]
            kwargs["shrink"] = True
        elif action is Action.SPLIT_TICKET:
            kwargs["split_reason"] = match.mode.name
            kwargs["evidence"] = match.evidence
        elif action is Action.KGR_FALLBACK:
            kwargs["kgr_query"] = match.evidence[:300]
            kwargs["mode_id"] = mode_id
        elif action is Action.DEMOTE_SKILL:
            kwargs["demoted_mode"] = mode_id
        elif action is Action.QUARANTINE_MEMORY:
            kwargs["quarantine_evidence"] = match.evidence
        elif action is Action.ESCALATE_HUMAN:
            kwargs["escalate_reason"] = (
                f"{mode_id} fired {n}x — exceeds repeat threshold "
                f"{self._REPEAT_ESCALATE_AT}"
                if n >= self._REPEAT_ESCALATE_AT
                else f"{mode_id} has no automatic policy"
            )

        if extra_kwargs:
            kwargs.update(extra_kwargs)

        rationale = (
            f"{mode_id} ({match.mode.name}) at stage={stage!r} "
            f"attempt={attempt} count={n} → {action.value}"
        )
        decision = Decision(
            action=action, rationale=rationale,
            mode_id=mode_id, kwargs=kwargs,
            halt=action is Action.ESCALATE_HUMAN,
        )
        self.history.append(decision)

        emit(self.log, "recovery.decision",
             stage=stage, attempt=attempt, mode_id=mode_id,
             count=n, action=action.value,
             evidence=match.evidence[:200])

        # Trip the circuit breaker on terminal actions so the
        # orchestrator's own .tripped check halts the run.
        if self.breakers is not None and action is Action.ESCALATE_HUMAN:
            self.breakers.trip(
                f"recovery_escalate_{mode_id.lower()}",
                f"recovery escalated {mode_id} after {n} hits",
            )

        return decision

    # --------------------------------------------------------------------
    # Convenience wrappers — orchestrator calls these instead of
    # constructing FailureMatches manually.

    def loop_check(self, *, key: str, output: str,
                   stage: str = "", attempt: int = 0) -> Decision | None:
        """Per-key 3x-same-output loop guard. Returns Decision when
        tripped, ``None`` otherwise."""
        from aiforge_core.aiforge_agents.runtime import detectors as det
        store = self.__dict__.setdefault("_loops", {})
        ld = store.get(key)
        if ld is None:
            ld = det.LoopDetector(window=3, mode_id="F-004")
            store[key] = ld
        m = ld.record(output)
        if m is None:
            return None
        return self.handle(m, stage=stage, attempt=attempt)

    def plan_depth_check(self, plan: dict[str, Any], *, stage: str = "planner",
                         attempt: int = 0,
                         max_depth: int = 12) -> Decision | None:
        from aiforge_core.aiforge_agents.runtime import detectors as det
        m = det.check_plan_depth(plan, max_depth=max_depth)
        if m is None:
            return None
        return self.handle(m, stage=stage, attempt=attempt)

    def token_budget_check(self, *, used: int, expected: int,
                           stage: str = "", attempt: int = 0,
                           multiplier: float = 2.0) -> Decision | None:
        from aiforge_core.aiforge_agents.runtime import detectors as det
        m = det.check_token_budget(used, expected=expected,
                                   multiplier=multiplier)
        if m is None:
            return None
        return self.handle(m, stage=stage, attempt=attempt)
