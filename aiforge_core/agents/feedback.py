"""Feedback archetype — post-execution judge.

Single-turn JSON ``{verdict: pass|fail|scope_violation, rationale}``.
``scope_violation`` outranks ``fail`` per the YAML rule: any write
outside the allowlist is a scope_violation regardless of test colour.

The model's judgment is additionally subject to a deterministic
*quality gate* (gap A1): if the run carries red typecheck/test signals
in session state, a model ``pass`` is downgraded to ``fail`` before the
verdict leaves this agent. See :mod:`aiforge_core.runtime.quality_gate`.
"""
from __future__ import annotations

import json
import logging

from aiforge_core.runtime import prompts, quality_gate

from . import _base

log = logging.getLogger("aiforge.agents.feedback")

ROLE = "feedback"
PROMPT = prompts.FEEDBACK
OUTPUT_KEY = "feedback_verdict"
TOOLS_FACTORY = None   # judge — tools forbidden by contract

# Verdict tokens, longest-first so ``scope_violation`` is matched before
# ``fail`` (the literal contains ``fail`` as a substring).
_VERDICT_TOKENS = ("scope_violation", "pass", "fail")


def _parse_model_verdict(raw) -> str:
    """Extract the verdict token from whatever the model wrote to state.

    Mirrors adk_runner's tolerant parsing: dict, JSON string, or plain
    leading-token text. Unknown → ``fail``.
    """
    if isinstance(raw, dict):
        return str(raw.get("verdict", "fail")).lower()
    if not isinstance(raw, str):
        return "fail"
    text = raw.strip()
    if not text:
        return "fail"
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("verdict"):
            return str(obj["verdict"]).lower()
    head = text.lstrip("`*_-> ").lower()
    for token in _VERDICT_TOKENS:
        if head.startswith(token):
            return token
    return "fail"


def make_quality_gate_after_callback():
    """Return an ADK ``after_agent_callback`` enforcing the quality gate.

    Reads ``tests_ok`` / ``typecheck_ok`` / ``lint_ok`` from session
    state (absent → ``None`` → gate stays pass), runs the model verdict
    through :func:`quality_gate.gate_verdict`, and rewrites
    ``state['feedback_verdict']`` only when the gate forces a downgrade.

    Soft-fail and backward-compatible: when no signals are present the
    verdict is left exactly as the model emitted it.
    """
    def _callback(*, callback_context, **_kw):
        try:
            state = callback_context.state
            gate = quality_gate.evaluate(
                typecheck_ok=state.get("typecheck_ok"),
                tests_ok=state.get("tests_ok"),
                lint_ok=state.get("lint_ok"),
                # Fix 3: a capped/incomplete Doer run (``doer_incomplete``)
                # hard-fails; the tests-declared-but-never-ran downgrade stays
                # behind AIFORGE_STRICT_TEST_GATE inside quality_gate.evaluate.
                doer_incomplete=state.get("doer_incomplete"),
                tests_declared=state.get("tests_declared"),
            )
            if gate["gate"] != "fail":
                return None
            raw = state.get("feedback_verdict")
            model_verdict = _parse_model_verdict(raw)
            gated = quality_gate.gate_verdict(model_verdict, gate)
            if gated != model_verdict:
                state["feedback_verdict"] = gated
                log.info(
                    "quality_gate downgraded feedback verdict %s -> %s (%s)",
                    model_verdict, gated, "; ".join(gate["reasons"]),
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("quality_gate callback: %s", exc)
        return None

    return _callback


def build(model_factory: _base.ModelFactory):
    agent = _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )
    # Chain the quality gate after the stage callback _base attached, so
    # the deterministic downgrade runs once the model has written its
    # verdict to state. ADK accepts a list of after-callbacks; preserve
    # any existing one (matches the pipeline.py merge convention).
    existing_after = agent.after_agent_callback
    merged_after: list = []
    if existing_after is not None:
        if isinstance(existing_after, list):
            merged_after.extend(existing_after)
        else:
            merged_after.append(existing_after)
    merged_after.append(make_quality_gate_after_callback())
    agent.after_agent_callback = merged_after
    return agent


__all__ = [
    "ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build",
    "make_quality_gate_after_callback",
]
