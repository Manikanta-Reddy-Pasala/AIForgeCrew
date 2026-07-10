"""Auto-authoring — the WRITE side of the OKR DAG.

Turns a chat/work session into graph nodes: an LLM reads the session and extracts
durable Objectives (goals), Key Results (measurable milestones), and Learnings
(rules/constraints); we allocate ids, dedupe against existing nodes by title, and
save each into its folder with the right edges. Also writes a plain ``session``
node from the execution ledger's working steps. Soft-fail everywhere — authoring
is best-effort background work.
"""
from __future__ import annotations

import os

from . import graph as _graph
from . import store as _store


def _slug(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _existing_objective_by_title(g, title: str) -> str | None:
    key = _slug(title)
    for nid, n in g.nodes.items():
        if n.get("type") == "objective" \
                and _slug((n.get("meta") or {}).get("title") or "") == key:
            return nid
    return None


_EXTRACT_SYS = (
    "Extract DURABLE goal-memory from a work session. Return objectives (long-"
    "lived goals), key_results (measurable milestones, each tied to an objective "
    "title), and learnings (rules/constraints discovered — a learning's scope is "
    "'global' or the objective title it applies to). Only extract things worth "
    "keeping across sessions; skip one-off chatter. Do NOT invent — use what the "
    "session shows. Empty lists are fine."
)


def extract_and_save(session_text: str, *, active_kr: str | None = None) -> dict:
    """LLM-extract objectives/KRs/learnings from ``session_text`` and save them
    as nodes (deduped by title). Returns a summary; never raises. Disable with
    AIFORGE_OKR_AUTHOR=0."""
    if os.environ.get("AIFORGE_OKR_AUTHOR", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return {"ok": False, "skipped": "disabled"}
    text = (session_text or "").strip()
    if len(text) < 40:
        return {"ok": True, "skipped": "too_short"}
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Obj(BaseModel):
            title: str
            context: str = ""

        class _KR(BaseModel):
            title: str
            objective_title: str = ""
            metrics: str = ""

        class _Learn(BaseModel):
            rule: str
            scope: str = "global"

        class _Extract(BaseModel):
            objectives: list[_Obj] = []
            key_results: list[_KR] = []
            learnings: list[_Learn] = []

        res = structured_complete(
            "learner",
            [{"role": "system", "content": _EXTRACT_SYS},
             {"role": "user", "content": text[:8000]}],
            _Extract, max_retries=1, max_tokens=1500, temperature=0.1)
    except Exception as exc:  # noqa: BLE001 — no model / bad json
        return {"ok": False, "error": str(exc)}

    g = _graph.build(force=True)
    title_to_oid: dict[str, str] = {}
    made = {"objectives": [], "key_results": [], "learnings": []}
    for o in res.objectives:
        if not o.title.strip():
            continue
        oid = _existing_objective_by_title(g, o.title) or title_to_oid.get(_slug(o.title))
        if not oid:
            r = _store.save_node("objective", None,
                                 {"title": o.title.strip(), "status": "active"},
                                 o.context.strip())
            oid = r.get("id")
            made["objectives"].append(oid)
        title_to_oid[_slug(o.title)] = oid
    # refresh so KRs/learnings can resolve just-created objectives
    g = _graph.build(force=True)
    for kr in res.key_results:
        if not kr.title.strip():
            continue
        oid = title_to_oid.get(_slug(kr.objective_title)) \
            or _existing_objective_by_title(g, kr.objective_title)
        meta = {"title": kr.title.strip(), "status": "in-progress"}
        if oid:
            meta["parent_objective"] = oid
        if kr.metrics.strip():
            meta["metrics"] = kr.metrics.strip()
        r = _store.save_node("key_result", None, meta, "")
        made["key_results"].append(r.get("id"))
    for ln in res.learnings:
        if not ln.rule.strip():
            continue
        sc = ln.scope.strip()
        if sc.lower() == "global" or not sc:
            scope: object = "global"
        else:
            oid = title_to_oid.get(_slug(sc)) or _existing_objective_by_title(g, sc)
            scope = [oid] if oid else "global"
        r = _store.save_node("learning", None, {"scope": scope}, ln.rule.strip())
        made["learnings"].append(r.get("id"))
    made["ok"] = True
    return made


def write_session_node(*, title: str, body: str,
                       linked_krs: list[str] | None = None) -> dict:
    """Write a ``session`` node (chronological log) from a run's steps, linked to
    the KRs it advanced (defaults to the active KR). Soft-fail."""
    krs = list(linked_krs or [])
    if not krs:
        act = _graph.get_active()
        if act:
            krs = [act]
    import datetime as _dt
    meta = {"date": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d"),
            "linked_krs": krs, "title": title}
    return _store.save_node("session", None, meta, body)


__all__ = ["extract_and_save", "write_session_node"]
