"""Extracting objectives, key results and learnings from a session and saving
them as OKF nodes."""
from __future__ import annotations

import os

from . import graph as _graph
from . import store as _store


def _slug(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _dedup_key(s: str) -> str:
    """Normalized fingerprint of a solution summary for duplicate detection:
    lowercased, punctuation stripped, whitespace collapsed, a 'DID:' prefix
    dropped. Two summaries with the same key are the same solution."""
    import re
    s = re.sub(r"^\s*did:\s*", "", (s or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s.lower())).strip()


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
    "title), and learnings (rules/constraints discovered). Only extract things "
    "worth keeping across sessions; skip one-off chatter. Do NOT invent — use "
    "what the session shows. Empty lists are fine.\n"
    "\n"
    "CLASSIFY each learning on TWO axes:\n"
    "1. scope — where it applies. Use exactly one of:\n"
    "   • 'global' — a rule that holds for ALL repos: a user preference, a "
    "cross-cutting convention, a decision about how to work generally.\n"
    "   • 'repo' — knowledge SPECIFIC to THIS repository: its folder/module "
    "layout, the build/test command that works here, entry points, a pattern or "
    "naming convention used in this codebase, a repo-specific gotcha.\n"
    "   • an objective title — a constraint that belongs to that goal.\n"
    "   When unsure between global and repo, prefer 'repo' if it names files/"
    "paths/commands of this codebase, else 'global'.\n"
    "2. topic — a SHORT kebab-case theme slug the learning is about (e.g. sync, "
    "auth, build, testing, error-handling, conventions, deploy). The theme is "
    "the cross-repo axis, orthogonal to scope."
)


def _topic_slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")[:40]


def _extract_okr(text: str):
    """One structured call: objectives, key results, learnings. Raises on a
    missing model / bad JSON — the caller turns that into a soft failure."""
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
        scope: str = "global"        # 'global' | 'repo' | an objective title
        topic: str = ""              # theme slug (cross-repo axis)

    class _Extract(BaseModel):
        objectives: list[_Obj] = []
        key_results: list[_KR] = []
        learnings: list[_Learn] = []

    return structured_complete(
        "learner",
        [{"role": "system", "content": _EXTRACT_SYS},
         {"role": "user", "content": text[:8000]}],
        _Extract, max_retries=1, max_tokens=1500, temperature=0.1)


def _save_objectives(objectives, g, title_to_oid: dict) -> list:
    made = []
    for o in objectives:
        if not o.title.strip():
            continue
        oid = (_existing_objective_by_title(g, o.title)
               or title_to_oid.get(_slug(o.title)))
        if not oid:
            r = _store.save_node("objective", None,
                                 {"title": o.title.strip(), "status": "active"},
                                 o.context.strip(), reindex=False)
            oid = r.get("id")
            made.append(oid)
        title_to_oid[_slug(o.title)] = oid
    return made


def _save_key_results(key_results, g, title_to_oid: dict) -> list:
    made = []
    for kr in key_results:
        if not kr.title.strip():
            continue
        oid = (title_to_oid.get(_slug(kr.objective_title))
               or _existing_objective_by_title(g, kr.objective_title))
        meta = {"title": kr.title.strip(), "status": "in-progress"}
        if oid:
            meta["parent_objective"] = oid
        if kr.metrics.strip():
            meta["metrics"] = kr.metrics.strip()
        # REUSE the same-concept KR file (same scope + same title) instead of
        # minting a fresh KR-NN each run — OKF 'one concept = one file'.
        krid = _store.find_by_concept("key_result", meta, kr.title.strip())
        r = _store.save_node("key_result", krid, meta, "", reindex=False)
        made.append(r.get("id"))
    return made


def _learning_scope(scope: str, rule: str, repo, g, title_to_oid: dict) -> dict:
    """The scope/workspace meta for one learning.

    Global is injected into EVERY turn of EVERY repo as a mandatory rule, so it
    must be EARNED, not defaulted into. An empty scope is missing information,
    and `repo` scope with no repo name is a project fact whose project we failed
    to resolve — neither is evidence of a universal truth. Only an explicit
    `global` verdict on text that names no concrete artifact keeps the scope.
    """
    low = scope.strip().lower()
    if low == "repo" and repo:
        # project-specific → segregates into projects/<repo>/ (workspace is
        # what store._scope_of keys on).
        return {"scope": f"repo:{repo}", "workspace": repo}
    if low in ("global", "") or (low == "repo" and not repo):
        from ..scope_guard import UNSCOPED, may_be_global
        if low == "global" and may_be_global(rule or ""):
            return {"scope": "global"}
        if repo:
            return {"scope": f"repo:{repo}", "workspace": repo}
        return {"scope": UNSCOPED}
    oid = (title_to_oid.get(_slug(scope))
           or _existing_objective_by_title(g, scope))
    return {"scope": [oid] if oid else "global"}


def _save_learnings(learnings, g, title_to_oid: dict, repo) -> list:
    made = []
    for ln in learnings:
        if not ln.rule.strip():
            continue
        meta = _learning_scope(ln.scope, ln.rule, repo, g, title_to_oid)
        topic = _topic_slug((getattr(ln, "topic", "") or "").strip())
        if topic:                                # theme axis (orthogonal)
            meta["category"] = topic
            meta["tags"] = [f"topic:{topic}"]
        # REUSE the same-concept learning file (same scope + same/near rule
        # text) instead of minting a fresh L-NN each run — this is the primary
        # fix for 'multiple files for the same topic': the learner ran twice
        # over similar work and produced L-01, L-07, L-13… for one rule.
        lid = _store.find_by_concept("learning", meta, ln.rule.strip())
        r = _store.save_node("learning", lid, meta, ln.rule.strip(),
                             reindex=False)
        made.append(r.get("id"))
    return made


def extract_and_save(session_text: str, *, _active_kr: str | None = None,
                     repo: str | None = None) -> dict:
    """LLM-extract objectives/KRs/learnings from ``session_text`` and save them
    as nodes (deduped by title). Each learning is CLASSIFIED by scope
    (global / this ``repo`` / an objective) and tagged with its topic, so
    repo-specific knowledge segregates into ``projects/<repo>/`` instead of
    piling into the global bucket. Returns a summary; never raises. Disable with
    AIFORGE_OKR_AUTHOR=0."""
    if os.environ.get("AIFORGE_OKR_AUTHOR", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return {"ok": False, "skipped": "disabled"}
    text = (session_text or "").strip()
    if len(text) < 40:
        return {"ok": True, "skipped": "too_short"}
    try:
        res = _extract_okr(text)
    except Exception as exc:  # noqa: BLE001 — no model / bad json
        return {"ok": False, "error": str(exc)}

    g = _graph.build(force=True)
    title_to_oid: dict[str, str] = {}
    made = {"objectives": _save_objectives(res.objectives, g, title_to_oid)}
    # refresh so KRs/learnings can resolve just-created objectives
    g = _graph.build(force=True)
    made["key_results"] = _save_key_results(res.key_results, g, title_to_oid)
    made["learnings"] = _save_learnings(res.learnings, g, title_to_oid, repo)
    if made["objectives"] or made["key_results"] or made["learnings"]:
        _store._write_index()          # one index rewrite for the whole batch
    made["ok"] = True
    return made
