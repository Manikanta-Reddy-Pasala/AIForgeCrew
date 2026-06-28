"""Mem0/Letta-style memory write-path (frontier gaps #1, #2, #5).

Today memory only gets what a caller explicitly hands it. This module
MINES a finished run for durable knowledge and reconciles it against what
is already stored, instead of blindly appending:

  #1 extract — LLM pulls durable, reusable facts out of a run trajectory
     (decisions, gotchas, conventions), dropping ephemera.
  #2 decide  — for each candidate, find the most-similar existing facts
     and decide ADD / UPDATE / DELETE / NOOP (dedup + contradiction
     resolution), then apply via the store's bi-temporal supersede /
     invalidate / touch primitives.
  #5 reflect — a one-paragraph "what was learned this run" summary saved
     as an Observation(kind='reflection').

LLM + embedder are INJECTED as callables so this stays decoupled from any
provider — the AIForgeCrew runtime passes its own client. Everything
soft-fails: a broken LLM/embed step must never lose a run or crash the
pipeline.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from . import store

log = logging.getLogger("aiforge.memory.consolidate")

# Injected callables:
#   llm_fn(system: str, user: str) -> str        (chat completion text)
#   embed_fn(text: str) -> list[float] | None    (1 vector)
LlmFn = Callable[[str, str], str]
EmbedFn = Callable[[str], "list[float] | None"]

_EXTRACT_SYS = (
    "You distil DURABLE, REUSABLE engineering memory from a finished "
    "task. Output ONLY a JSON array of short fact strings. A good fact is "
    "a decision, a gotcha, a convention, a non-obvious cause→fix, or a "
    "stable property of the code/system that will help a FUTURE task. "
    "Drop ephemera: progress chatter, this-run-only details, restating "
    "the ticket, secrets. 0-8 facts. Prefix an architectural choice with "
    "'DECISION: '. Output [] when nothing is worth keeping."
)

_DECIDE_SYS = (
    "You maintain a memory store. Given a NEW candidate fact and the most "
    "SIMILAR existing facts (with ids), choose ONE action:\n"
    "  ADD    — genuinely new; not covered by any existing fact.\n"
    "  NOOP   — already captured by an existing fact (semantic duplicate); "
    "give its id as target.\n"
    "  UPDATE — refines/corrects an existing fact; give the id to supersede "
    "as target (the new fact replaces it).\n"
    "  DELETE — the new fact says an existing fact is NO LONGER TRUE with no "
    "replacement; give that id as target.\n"
    "Return STRICT JSON: {\"action\":\"ADD|NOOP|UPDATE|DELETE\","
    "\"target\":\"<existing id or empty>\",\"reason\":\"<short>\"}."
)

_REFLECT_SYS = (
    "In 1-3 sentences, summarise what was LEARNED or DECIDED in this run "
    "that a future engineer on this repo should know. Concrete, no "
    "preamble. If nothing notable, output exactly 'NONE'."
)


def _safe_json(text: str):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return None


def extract_facts(trajectory_text: str, *, llm_fn: LlmFn,
                  max_facts: int = 8) -> list[str]:
    """LLM-extract durable facts from a run trajectory. Soft-fails to []."""
    if not (trajectory_text or "").strip():
        return []
    try:
        out = llm_fn(_EXTRACT_SYS, trajectory_text[:12000])
    except Exception as exc:  # noqa: BLE001
        log.warning("consolidate.extract failed: %s", exc)
        return []
    data = _safe_json(out)
    if not isinstance(data, list):
        return []
    facts = [str(x).strip() for x in data if str(x).strip()]
    return facts[:max_facts]


def decide_action(new_fact: str, similar: list[dict], *,
                  llm_fn: LlmFn) -> dict:
    """Decide ADD/NOOP/UPDATE/DELETE for ``new_fact`` vs ``similar`` (each
    ``{id, text}``). No similar → ADD without an LLM call. Soft-fails to
    ADD (appending is the safe default — never lose a fact)."""
    if not similar:
        return {"action": "ADD", "target": "", "reason": "no similar fact"}
    listing = "\n".join(f"- [{s.get('id')}] {s.get('text','')}"
                        for s in similar)
    user = f"NEW FACT:\n{new_fact}\n\nSIMILAR EXISTING FACTS:\n{listing}"
    try:
        out = llm_fn(_DECIDE_SYS, user)
    except Exception as exc:  # noqa: BLE001
        log.warning("consolidate.decide failed: %s", exc)
        return {"action": "ADD", "target": "", "reason": "decide error"}
    d = _safe_json(out) or {}
    action = str(d.get("action", "ADD")).upper()
    if action not in ("ADD", "NOOP", "UPDATE", "DELETE"):
        action = "ADD"
    return {"action": action, "target": str(d.get("target", "") or ""),
            "reason": str(d.get("reason", "") or "")}


def reflect(trajectory_text: str, *, llm_fn: LlmFn) -> str:
    """One-paragraph reflection. '' when nothing notable. Soft-fails to ''."""
    if not (trajectory_text or "").strip():
        return ""
    try:
        out = (llm_fn(_REFLECT_SYS, trajectory_text[:12000]) or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("consolidate.reflect failed: %s", exc)
        return ""
    return "" if out.upper() == "NONE" else out


def apply_decision(driver, *, repo: str, fact: str, decision: dict,
                   embed_vec: "list[float] | None", author: str,
                   tags: list[str] | None = None,
                   event_time: float | None = None,
                   importance: float = 0.6) -> dict:
    """Apply one decision via the store's bi-temporal primitives.

    ADD→new obs · UPDATE→new obs superseding target · DELETE→invalidate
    target (no replacement) · NOOP→touch target (re-upsert dedupes → bumps
    seen_count). Returns ``{action, id?}``."""
    action = decision.get("action", "ADD")
    target = (decision.get("target") or "").strip()
    kind = "decision" if fact.upper().startswith("DECISION:") else "learning"
    tags = list(tags or [])

    if action == "NOOP" and target:
        # Re-upsert the SAME text → dedupe path bumps seen_count.
        return {"action": "NOOP",
                **store.upsert_observation(driver, repo=repo, text=fact,
                                           kind=kind, author=author,
                                           tags=tags, embed_vec=embed_vec,
                                           event_time=event_time,
                                           importance=importance)}
    if action == "DELETE" and target:
        store.invalidate_observation(driver, repo=repo, node_id=target,
                                     reason=decision.get("reason", ""))
        return {"action": "DELETE", "id": target}
    supersedes = [target] if (action == "UPDATE" and target) else None
    res = store.upsert_observation(
        driver, repo=repo, text=fact, kind=kind, author=author, tags=tags,
        embed_vec=embed_vec, event_time=event_time, importance=importance,
        supersedes=supersedes,
    )
    return {"action": "UPDATE" if supersedes else "ADD", **res}


def consolidate(driver, *, repo: str, trajectory_text: str,
                llm_fn: LlmFn, embed_fn: EmbedFn, author: str = "learner",
                tags: list[str] | None = None,
                event_time: float | None = None,
                top_k: int = 5) -> dict:
    """Full write-path: extract → decide → apply, then write a reflection.

    Returns ``{facts: int, actions: [...], reflection: id|None}``. Soft-
    fails per step — partial progress is fine, a failure never raises."""
    facts = extract_facts(trajectory_text, llm_fn=llm_fn)
    actions: list[dict] = []
    for fact in facts:
        try:
            vec = None
            try:
                vec = embed_fn(fact)
            except Exception:  # noqa: BLE001
                vec = None
            similar: list[dict] = []
            if vec:
                similar = [
                    {"id": r.get("id"), "text": r.get("text", "")}
                    for r in store.recall_observations(
                        driver, repo=repo, query_vec=vec, k=top_k)
                ]
            decision = decide_action(fact, similar, llm_fn=llm_fn)
            actions.append(apply_decision(
                driver, repo=repo, fact=fact, decision=decision,
                embed_vec=vec, author=author, tags=tags,
                event_time=event_time))
        except Exception as exc:  # noqa: BLE001
            log.warning("consolidate.apply failed for %r: %s", fact[:60], exc)
    # Reflection — its own observation, importance-boosted.
    refl_id = None
    summary = reflect(trajectory_text, llm_fn=llm_fn)
    if summary:
        try:
            rvec = None
            try:
                rvec = embed_fn(summary)
            except Exception:  # noqa: BLE001
                rvec = None
            r = store.upsert_observation(
                driver, repo=repo, text=summary, kind="reflection",
                author=author, tags=(tags or []) + ["reflection"],
                embed_vec=rvec, event_time=event_time, importance=0.7)
            refl_id = r.get("id")
        except Exception as exc:  # noqa: BLE001
            log.warning("consolidate.reflect write failed: %s", exc)
    return {"facts": len(facts), "actions": actions, "reflection": refl_id}


__all__ = ["extract_facts", "decide_action", "reflect", "apply_decision",
           "consolidate"]
