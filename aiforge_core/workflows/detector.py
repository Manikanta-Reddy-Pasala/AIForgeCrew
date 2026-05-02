"""Auto-detect which route (code task vs. named workflow) a ticket should take.

Score-based — each workflow declares its triggers in
:class:`WorkflowSpec.triggers`. The detector evaluates every workflow
against the ticket, returns the highest-scoring match above its
threshold. Falls back to ``code`` when nothing scores high enough.

Trigger keys (all optional, AND'd within a single workflow):

* ``keywords_any: [str, ...]``     — case-insensitive substring; +0.5 if any hit
* ``keywords_all: [str, ...]``     — every entry must hit; +0.4 if all hit
* ``attachments_all: [str, ...]``  — required attachment roles; +0.5 if all present
* ``attachment_any:  [str, ...]``  — at least one of these roles; +0.3 if hit
* ``intent_action_in: [str, ...]`` — intent.action membership; +0.2 if hit
* ``min_confidence: float``        — score floor; default 0.6

Resulting :class:`TicketRoute` carries ``confidence`` so the UI can
show 'detected (high)' vs 'detected (low — please confirm)'.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .registry import REGISTRY, WorkflowSpec


@dataclass
class TicketRoute:
    kind: str                              # 'code' or 'workflow'
    workflow_id: str | None = None
    confidence: float = 1.0                # 0..1
    source: str = "auto"                   # 'auto' or 'manual'
    rationale: str = ""                    # human-readable why


_DEFAULT_MIN_CONF: float = 0.6


def _score(spec: WorkflowSpec, *,
           text: str,
           attachments: Iterable[str],
           intent_action: str | None) -> tuple[float, list[str]]:
    """Return (score, hit_reasons) for one spec against one ticket."""
    text_lc = text.lower()
    atts = set(attachments or [])
    score = 0.0
    reasons: list[str] = []

    triggers = spec.triggers or {}
    kws_any = triggers.get("keywords_any") or []
    if kws_any and any(kw.lower() in text_lc for kw in kws_any):
        score += 0.5
        reasons.append(f"keyword:{next(kw for kw in kws_any if kw.lower() in text_lc)}")

    kws_all = triggers.get("keywords_all") or []
    if kws_all and all(kw.lower() in text_lc for kw in kws_all):
        score += 0.4
        reasons.append(f"keywords_all:{','.join(kws_all)}")

    atts_all = triggers.get("attachments_all") or []
    if atts_all and all(a in atts for a in atts_all):
        score += 0.5
        reasons.append(f"attachments_all:{','.join(atts_all)}")

    atts_any = triggers.get("attachments_any") or []
    if atts_any and any(a in atts for a in atts_any):
        score += 0.3
        reasons.append(f"attachments_any:{','.join(atts_any)}")

    actions = triggers.get("intent_action_in") or []
    if intent_action and actions and intent_action in actions:
        score += 0.2
        reasons.append(f"intent_action:{intent_action}")

    return min(score, 1.0), reasons


def detect_route(*,
                 body: str = "",
                 title: str = "",
                 attachments: Iterable[str] | None = None,
                 intent: dict | None = None) -> TicketRoute:
    """Score every registered workflow against the ticket; pick the best.

    ``intent`` is the ``Intent`` dict produced by :mod:`aiforge_core.intent`,
    used opportunistically — detector still works on raw title/body when
    no intent is available (e.g. tests, IntentLayer disabled).
    """
    text = f"{title}\n{body}"
    intent_action = (intent or {}).get("action") if intent else None
    atts = list(attachments or [])

    best: tuple[float, WorkflowSpec, list[str]] | None = None
    for spec in REGISTRY.values():
        score, reasons = _score(spec, text=text, attachments=atts,
                                intent_action=intent_action)
        if score <= 0:
            continue
        threshold = float(
            (spec.triggers or {}).get("min_confidence", _DEFAULT_MIN_CONF)
        )
        if score < threshold:
            continue
        if best is None or score > best[0]:
            best = (score, spec, reasons)

    if best is None:
        return TicketRoute(
            kind="code", confidence=1.0, source="auto",
            rationale="no workflow trigger matched",
        )

    score, spec, reasons = best
    return TicketRoute(
        kind="workflow",
        workflow_id=spec.id,
        confidence=round(score, 2),
        source="auto",
        rationale="; ".join(reasons),
    )


def preview(body: str, title: str = "",
            attachments: list[str] | None = None,
            intent: dict | None = None) -> dict[str, Any]:
    """UI helper: return BOTH the picked route AND the score for every
    workflow above zero. Lets the UI show alternatives.
    """
    text = f"{title}\n{body}"
    intent_action = (intent or {}).get("action") if intent else None
    atts = list(attachments or [])

    candidates: list[dict] = []
    for spec in REGISTRY.values():
        score, reasons = _score(spec, text=text, attachments=atts,
                                intent_action=intent_action)
        if score <= 0:
            continue
        threshold = float(
            (spec.triggers or {}).get("min_confidence", _DEFAULT_MIN_CONF)
        )
        candidates.append({
            "workflow_id": spec.id,
            "label": spec.label,
            "score": round(score, 2),
            "threshold": threshold,
            "above_threshold": score >= threshold,
            "reasons": reasons,
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)

    chosen = detect_route(body=body, title=title, attachments=atts,
                          intent=intent)
    return {
        "chosen": {
            "kind": chosen.kind,
            "workflow_id": chosen.workflow_id,
            "confidence": chosen.confidence,
            "source": chosen.source,
            "rationale": chosen.rationale,
        },
        "candidates": candidates,
    }
