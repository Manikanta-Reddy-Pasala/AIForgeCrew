"""Pure parsing / classification helpers for pipeline output.

Feedback-verdict extraction, rationale flattening, live-verifier JSON
parsing, the enhancer block sentinel, plus the two small run-shape
heuristics (pipeline deadline, read-only-ticket detection). All
side-effect-free except :func:`_record_verdict_event`, which writes an
audit row via the tickets store.
"""
from __future__ import annotations

import json
import os

from ._base import (
    _REASON_DEFAULT_FAIL,
    _REASON_DEFAULT_PASS,
    _REASON_MAX_CHARS,
    _VERDICT_TOKENS,
    log,
    tickets_mod,
)


def _pipeline_deadline_s() -> float:
    """Overall wall-clock ceiling for one pipeline / single-agent run.

    The per-call LLM timeout (900s) and max_llm_calls (600) each bound ONE
    dimension, but neither stops a run that stalls WITHOUT tripping them — an
    async await that never resolves, a graph that spins below the call cap, a
    stage waiting on output that never comes. Without an outer deadline such a
    run waits forever and the ticket never lands. asyncio.timeout cancels the
    in-flight run_async so the caller recovers partial state and marks the
    ticket blocked instead of hanging. Default 90 min (a healthy full+replan
    run is well under that); 0/negative disables. Tune AIFORGE_PIPELINE_DEADLINE_S.
    """
    try:
        v = float(os.environ.get("AIFORGE_PIPELINE_DEADLINE_S", "5400"))
    except ValueError:
        v = 5400.0
    return v


def _ticket_looks_readonly(ticket) -> bool:
    """True when the ticket's intent is READ/analyze/comment, not a code change —
    so the pipeline should not scaffold tests or open a PR. Mirrors the chat
    ``_looks_like_analysis`` heuristic (ask-verb present, no change-verb) over the
    title+body. Conservative: any change verb → treated as a code ticket."""
    import re as _re
    text = f"{getattr(ticket, 'title', '') or ''} {getattr(ticket, 'body', '') or ''}".lower()
    if not text.strip():
        return False
    ask = _re.search(r"\b(analy[sz]e|explain|describe|summar[iy][sz]e|review|"
                     r"investigate|comment|assign|triage|document|audit|research|"
                     r"find (out|the)|look (into|at)|check|get me|report on)\b", text)
    change = _re.search(r"\b(fix|create|build|implement|add|write|refactor|rename|"
                        r"delete|remove|update|generat|make|modify|patch|scaffold|"
                        r"migrat|upgrade|bump|integrat)\b", text)
    return bool(ask and not change)


def _extract_reason(state: dict, verdict: str) -> str:
    """Pull the post-verdict rationale line out of the Feedback output.

    The Feedback prompt asks the model to put the verdict token on
    line 1 and a short rationale on line 2+. This function returns
    that rationale flattened to a single line, trimmed to
    :data:`_REASON_MAX_CHARS` so the audit trail can't be bloated.

    Tolerated shapes mirror :func:`_extract_verdict`:
      1. raw string ``"<token>\\n<reason>..."`` → returns reason
      2. dict ``{"verdict": ..., "rationale": "..."}`` → returns rationale
      3. anything else / no rationale → role-appropriate default

    The verdict token itself is stripped from the head so we don't
    double-print it (``"pass: pass — looks good"`` → ``"pass: looks good"``).
    """
    raw = state.get("feedback_verdict")
    text: str | None = None
    if isinstance(raw, dict):
        # Legacy JSON dict — both ``rationale`` and ``reason`` seen in
        # the wild; ``reason`` is the new canonical key (matches the
        # ticket_events column the operators query).
        text = raw.get("rationale") or raw.get("reason")
    elif isinstance(raw, str) and raw.strip():
        s = raw.strip()
        # Legacy JSON string — try once, fall through to plain text on
        # parse fail rather than 500ing.
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                text = obj.get("rationale") or obj.get("reason")
        if text is None:
            # Plain leading-token format: drop line 1, take the rest.
            head = s.lstrip("`*_-> ")
            for token in _VERDICT_TOKENS:
                if head.lower().startswith(token):
                    head = head[len(token):]
                    break
            # Anything after the verdict token is the rationale; flatten
            # newlines and tabs so the audit row is single-line.
            text = head.strip(" :—-\n\t").replace("\n", " ").replace("\t", " ")

    if not text:
        return _REASON_DEFAULT_PASS if verdict == "pass" else _REASON_DEFAULT_FAIL

    # Collapse runs of whitespace so the audit body is compact.
    text = " ".join(text.split())
    if len(text) > _REASON_MAX_CHARS:
        text = text[: _REASON_MAX_CHARS - 1].rstrip() + "…"
    return text


def _record_verdict_event(ticket_id: int, verdict: str, reason: str) -> None:
    """Persist a ``verdict_attempt`` row in ``ticket_events``.

    Called once per ticket after the SequentialAgent run resolves its
    final session state. The row schema is:

      kind        = 'verdict_attempt'
      agent_role  = 'feedback'
      body        = '<verdict>: <reason>'
      metadata    = {'verdict': ..., 'reason': ...}

    Operators query this row to see WHY a Doer-Feedback loop
    converged (or didn't) — the prior ``status_change`` rows only
    captured the eventual ticket status, not the reasoning.

    Failures are swallowed: the audit trail is best-effort, we never
    want a Postgres hiccup to block the runner from finalising the
    ticket status. The exception is logged so an operator can spot a
    persistent DB-grant problem.
    """
    body = f"{verdict}: {reason}"
    try:
        tickets_mod.add_event(
            ticket_id, "feedback", "verdict_attempt", body,
            {"verdict": verdict, "reason": reason},
        )
    except Exception as exc:  # pragma: no cover — best-effort audit
        log.warning("ticket_id=%s failed to persist verdict_attempt: %s",
                    ticket_id, exc)


def _extract_live_verifier(state: dict) -> dict | None:
    """Pull the live_verifier verdict out of pipeline state.

    The agent is told to emit a fenced ```json``` block at the end of
    its response containing ``{"ok": bool, "rationale": "...", ...}``.
    Parses the LAST such block in ``state['live_verifier_verdict']``.
    Returns ``None`` when the stage didn't run or the JSON couldn't be
    parsed — caller treats that as "no veto" rather than blocking on
    a parser hiccup.
    """
    # A run that ABORTED (wall-clock deadline / mid-run error) never produced a
    # verdict — treat that as a VETO, not a silent pass. Without this, a stalled
    # behavioral verification returns partial state with no "ok" key → None →
    # "no veto" → the ticket ships unverified.
    if state.get("_pipeline_abort"):
        return {"ok": False,
                "rationale": f"verification aborted ({state['_pipeline_abort']}) "
                             "— treated as a veto, not a pass"}
    raw = state.get("live_verifier_verdict")
    if isinstance(raw, dict):
        return raw if "ok" in raw else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    # Strip ```json fences then try whole-text + last balanced object.
    import re as _re
    fenced = _re.findall(
        r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.DOTALL,
    )
    candidates = fenced[::-1]  # prefer the last (final answer)
    if text.startswith("{"):
        candidates.append(text)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def _enhancer_block_reason(state: dict | None) -> str | None:
    """``None`` unless the Enhancer refused to enhance (see the
    ``ENHANCE_BLOCKED: <reason>`` sentinel contract in
    ``aiforge_core/runtime/prompts/enhancer.py``) — tickets are unattended,
    so this stands in for the clarifying question a chat agent could ask a
    human. Returns the trimmed reason text when blocked."""
    body = (state or {}).get("enhanced_body") or ""
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text.startswith("ENHANCE_BLOCKED"):
        return None
    return (text.split(":", 1)[-1].strip()[:300]
            or "ticket body too vague for the enhancer to act on")


def _extract_verdict(state: dict) -> str:
    """Pull the Feedback verdict out of pipeline state.

    The new Feedback prompt asks for a leading token (``pass`` /
    ``fail`` / ``scope_violation``) followed by an optional rationale
    line — much more robust than strict JSON for local models (qwen
    etc.) which routinely wrap responses in prose.

    Tolerated shapes (in order):
      1. raw string starting with one of the tokens
      2. JSON-with-``verdict``-key (legacy — still emitted by some
         models)
      3. anything else → ``fail``

    ``scope_violation`` is checked before ``fail`` because the literal
    string contains ``fail`` as a substring; without the order rule a
    model that emits ``scope_violation`` would be parsed as ``fail``.
    """
    raw = state.get("feedback_verdict")
    if isinstance(raw, dict):
        return str(raw.get("verdict", "fail")).lower()
    if not isinstance(raw, str):
        return "fail"

    text = raw.strip()
    if not text:
        return "fail"

    # Legacy JSON path — kept for tickets ran on older prompt revisions.
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("verdict"):
            return str(obj["verdict"]).lower()

    # New prompt path — leading token decides. Strip any markdown-y
    # wrapping the model might add despite the rules.
    head = text.lstrip("`*_-> ").lower()
    for token in _VERDICT_TOKENS:
        if head.startswith(token):
            return token
    return "fail"


def _extract_verifier(state: dict) -> str | None:
    """Grab the verifier verdict (``pass``/``reject``) when present."""
    raw = state.get("verifier_verdict")
    if isinstance(raw, dict):
        return raw.get("verdict")
    return None
