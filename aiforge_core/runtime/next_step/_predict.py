"""One capped call: what does this user probably want next?

Modelled on ``rule_capture._classify`` deliberately — same capped single call on
a cheap role, same confidence floor, same fail-open on every path. That module
is this codebase's precedent for "an always-on LLM pass that must never break a
turn", and copying its shape is cheaper than rediscovering its lessons.

The prompt carries three things: what the user said, what the agent actually did
about it, and the last few predictions this user ACCEPTED in this repo. The
third is the entire learning mechanism — no training and no embeddings, just
showing the model what this person has said yes to before.
"""
from __future__ import annotations

import logging
import os
import uuid

log = logging.getLogger("aiforge.next_step")

_SYS = (
    "You predict the ONE next action a developer most likely wants, given what "
    "they just asked and what was just done for them. Reply with ONLY a JSON "
    'object: {"action":"<one imperative sentence>","tool":"<tool name or '
    'empty>","args":{...},"confidence":0.0-1.0,"rationale":"<one clause>"}. '
    "Be conservative: if the next step is not strongly implied by what just "
    "happened, return a confidence below 0.5. Never propose an action that "
    "deletes data, pushes code, deploys, or spends money."
)

_MAX_EXAMPLES = 5
_DEFAULT_TIMEOUT_S = 10


def _disabled() -> bool:
    return (os.environ.get("AIFORGE_PREDICT_DISABLE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _timeout() -> int:
    try:
        return int(os.environ.get("AIFORGE_PREDICT_TIMEOUT_S") or _DEFAULT_TIMEOUT_S)
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _llm(role: str, messages: list, **kw) -> str:
    """Indirection so a test can replace the call without touching the network.

    Delegates to ``rule_capture``'s helper rather than building a second client:
    that one already knows how this codebase reaches a model role.
    """
    from aiforge_core.runtime.rule_capture import _llm_complete

    return _llm_complete(role, messages, **kw)


def _examples(repo: str) -> str:
    from aiforge_core.runtime.next_step import _store

    try:
        rows = _store.accepted(repo, limit=_MAX_EXAMPLES)
    except Exception:  # noqa: BLE001 — no history is not a reason to predict nothing
        return ""
    if not rows:
        return ""
    lines = "\n".join(f"- after {r.get('trigger')}: {r.get('action')}" for r in rows)
    return f"\n\nThis user previously accepted:\n{lines}"


def _user_prompt(ctx: dict) -> str:
    return (f"They said: {str(ctx.get('message') or '')[:2000]}\n"
            f"What was just done: {str(ctx.get('did') or '')[:1000]}"
            f"{_examples(str(ctx.get('repo') or ''))}")


def _parse(raw: str) -> dict | None:
    """The model's reply as a normalised dict, or None. Never raises."""
    from aiforge_core.runtime.rule_capture import _classify

    try:
        obj = _classify._extract_json(raw or "")
    except Exception:  # noqa: BLE001 — unparseable is simply no prediction
        return None
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action") or "").strip().replace("\n", " ")
    if not action:
        return None
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError):
        # A prediction with no confidence cannot be gated, and an ungated
        # prediction is the one thing this design does not allow.
        return None
    args = obj.get("args")
    return {"id": f"p-{uuid.uuid4().hex[:8]}",
            "action": action[:300],
            "tool": str(obj.get("tool") or "").strip(),
            "args": args if isinstance(args, dict) else {},
            "confidence": confidence,
            "rationale": str(obj.get("rationale") or "").strip()[:300]}


def raw_prediction(ctx: dict) -> dict | None:
    """The model's answer, normalised, or None. Never raises."""
    if _disabled():
        return None
    role = os.environ.get("AIFORGE_PREDICT_ROLE", "enhancer")
    try:
        raw = _llm(role,
                   [{"role": "system", "content": _SYS},
                    {"role": "user", "content": _user_prompt(ctx or {})}],
                   max_tokens=250, temperature=0.0, timeout_s=_timeout())
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        log.debug("next_step: prediction call failed (none): %s", exc)
        return None
    return _parse(raw or "")


__all__ = ["raw_prediction"]
