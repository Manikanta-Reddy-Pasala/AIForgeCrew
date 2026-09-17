"""Auto-authoring — the WRITE side of the OKR DAG.

Turns a chat/work session into graph nodes: an LLM reads the session and extracts
durable Objectives (goals), Key Results (measurable milestones), and Learnings
(rules/constraints); we allocate ids, dedupe against existing nodes by title, and
save each into its folder with the right edges. Also writes a plain ``session``
node from the execution ledger's working steps. Soft-fail everywhere — authoring
is best-effort background work.
"""
from __future__ import annotations

import logging
import os

from . import graph as _graph
from . import store as _store
from ._author_okr import (  # noqa: F401  # re-exported
    _EXTRACT_SYS,
    _dedup_key,
    _existing_objective_by_title,
    _extract_okr,
    _learning_scope,
    _save_key_results,
    _save_learnings,
    _save_objectives,
    _slug,
    _topic_slug,
    extract_and_save,
)
from ._author_reclassify import (  # noqa: F401  # re-exported
    _RECLASSIFY_SYS,
    _RECLASSIFY_USER_PREAMBLE,
    _apply_moves,
    _reclassify_decisions,
    _reclassify_plan,
    _repo_name_match,
    _trash_noise,
    reclassify_global_learnings,
)
from ._author_solutions import (  # noqa: F401  # re-exported
    _append_solution_log,
    _clean_list,
    _existing_solution,
    _solution_meta,
    record_solution,
    write_session_node,
)
from ._author_topics import (  # noqa: F401  # re-exported
    _BODY_CHARS,
    __all__,
    _body_for,
    _brief_facts_by_topic,
    _create_topic_node,
    _fact_lines,
    _learning_by_topic,
    _topic_scope,
    _update_topic_node,
    migrate_from_briefs,
    sync_briefs_to_nodes,
)

_log = logging.getLogger("aiforge.okf")


def _set_scalars(meta: dict, pairs) -> None:
    """Scalars OVERWRITE when provided, and are left alone when blank."""
    for k, v in pairs:
        if v and str(v).strip():
            meta[k] = str(v).strip()


def _union_into(meta: dict, key: str, new, cap: int = 30) -> None:
    """List fields UNION so the card accretes knowledge across sessions instead
    of churning."""
    cur = list(meta.get(key) or [])
    for x in (new or []):
        x = str(x).strip()
        if x and x not in cur:
            cur.append(x)
    if cur:
        meta[key] = cur[:cap]


# NOSONAR (S107) — the parameters ARE the repo card's fields, one per
# section of the rendered note. They are all optional and independent
# (scalars overwrite, lists union), so a params object would just be this
# list with an extra name in front of it.
def record_repo_profile(workspace: str, *, stack: str = "",  # NOSONAR
                        build: str = "",
                        test: str = "", run: str = "", structure: str = "",
                        entry_points=None, deploy: str = "", services=None,
                        tables=None, gotchas=None, conventions=None,
                        scripts=None, workflows=None, body: str = "",
                        date: str = "") -> dict:
    """UPSERT the ONE canonical ``repo`` card for ``workspace`` (id
    R-<slug>) — the detailed hub: how to build/test/run it, structure, deploy,
    connected services/tables, gotchas, and its scripts/workflows. Scalars
    overwrite when provided; list fields UNION so the card accretes knowledge
    across sessions instead of churning. Lives at projects/<repo>/repo/. Soft-
    fail."""
    try:
        ws = (workspace or "").strip()
        if not ws:
            return {"ok": False, "error": "no workspace"}
        nid = "R-" + _slug(ws).replace(" ", "-")
        existing = next((d for d in _store.load_all(ws)
                         if d.get("type") == "repo" and d.get("id") == nid), None)
        meta = dict((existing or {}).get("meta") or {})
        meta["workspace"] = ws
        meta["scope"] = f"repo:{ws}"
        meta.setdefault("title", ws)
        _set_scalars(meta, (("stack", stack), ("build", build), ("test", test),
                            ("run", run), ("structure", structure),
                            ("deploy", deploy), ("timestamp", date)))
        for key, values in (("entry_points", entry_points),
                            ("services", services), ("tables", tables),
                            ("gotchas", gotchas), ("conventions", conventions),
                            ("scripts", scripts), ("workflows", workflows)):
            _union_into(meta, key, values)
        newbody = (body or "").strip() or (existing or {}).get("body") or ""
        return _store.save_node("repo", nid, meta, newbody)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _existing_node(node_type: str, workspace: str, match) -> dict | None:
    """The node of ``node_type`` in ``workspace`` that ``match`` accepts."""
    for d in _store.load_all(workspace or None):
        if d.get("type") != node_type:
            continue
        m = d.get("meta") or {}
        if (m.get("workspace") or "") == (workspace or "") and match(m):
            return d
    return None


def _scoped_meta(workspace: str, date: str, about, base: dict) -> dict:
    """``base`` plus the repo scope and timestamp every record_* node carries."""
    meta = dict(base)
    if workspace:
        meta["workspace"] = workspace
        meta["scope"] = f"repo:{workspace}"
    meta["about"] = list(about or [])
    if date:
        meta["timestamp"] = date
    return meta


def record_script(*, name: str, lang: str, purpose: str = "", path: str = "",
                  run: str = "", workspace: str = "", about=None,
                  body: str = "", date: str = "") -> dict:
    """Record a reusable shell/python ``script`` node (what it does + how to run
    it), scoped to its repo. Deduped by (workspace, name). Soft-fail."""
    try:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "no name"}
        lang = "python" if "py" in (lang or "").lower() else "shell"
        dup = _existing_node("script", workspace, lambda m: m.get("name") == name)
        if dup is not None:
            return {"ok": True, "id": dup.get("id"), "deduped": True}
        meta = _scoped_meta(workspace, date, about,
                            {"name": name, "lang": lang,
                             "title": f"{name} ({lang})"})
        _set_scalars(meta, (("path", path), ("run", run)))
        if purpose:
            meta["purpose"] = purpose.strip()[:200]
        return _store.save_node("script", None, meta, body or purpose)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def record_task(*, title: str, workspace: str = "", about=None, body: str = "",
                tags=None, date: str = "") -> dict:
    """Record a small-task recipe (``task`` node) — 'how to do X in this repo',
    steps in the body. Deduped by (workspace, normalized title). Soft-fail."""
    try:
        title = (title or "").strip()
        if not title:
            return {"ok": False, "error": "no title"}
        key = _dedup_key(title)
        dup = _existing_node(
            "task", workspace, lambda m: _dedup_key(m.get("title") or "") == key)
        if dup is not None:
            return {"ok": True, "id": dup.get("id"), "deduped": True}
        meta = _scoped_meta(workspace, date, about, {"title": title})
        if tags:
            meta["tags"] = list(tags)
        return _store.save_node("task", None, meta, body or title)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _fact_buckets(learns: list[dict]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for d in learns:
        cat = str((d.get("meta") or {}).get("category") or "notes").lower()
        buckets.setdefault(cat, []).append(
            (d.get("body") or "").strip().lstrip("- ").strip())
    return buckets


def _structure_note(buckets: dict[str, list[str]]) -> str:
    """A repo's learnings are FACTS, not clean commands — so don't guess a
    build/test COMMAND from them (that mislabels 'sync retries…' as a test cmd).
    Only a genuine structure note is lifted; build/test/run fill in properly via
    the learner hook when a real command (topic: build/testing) is discovered."""
    for cat, facts in buckets.items():
        if facts and ("structure" in cat or "architecture" in cat
                      or "layout" in cat):
            return facts[0][:200]
    return ""


def build_repo_profiles() -> dict:
    """Seed/refresh each project's ``repo`` card by AGGREGATING its learnings —
    pull build/test/structure from category-matched learnings, collect the rest
    as gotchas. A deterministic starting card the learner then refines. One card
    per project scope. Soft-fail."""
    made = 0
    for ws in _store.okr_scopes():
        learns = [d for d in _store.load_all(ws) if d.get("type") == "learning"]
        if not learns:
            continue
        buckets = _fact_buckets(learns)
        r = record_repo_profile(
            ws, structure=_structure_note(buckets),
            gotchas=[f for facts in buckets.values() for f in facts if f][:12],
            body="Auto-built from this repo's learnings; build/test/run fill in "
                 "as they're discovered.")
        if r.get("ok"):
            made += 1
    return {"ok": True, "profiles": made}
