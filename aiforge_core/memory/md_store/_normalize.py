"""md_store internals: put every brief and OKF topic node into one clean form.

Deterministic (no model), and the first step of every compaction pass, so the
evening run corrects what earlier writers left instead of needing a one-off
script per box: stray nested bullets ("- - X", which grew one level per rev),
an item joined with its own copies, and exact duplicates ("[topic] X" next to
"X", case / spacing / trailing-full-stop variants). The cleaning rules live in
work_notes.scrub_items; this pass only re-renders files whose lists change, so
an already-clean store is not rewritten (no timestamp churn, nothing to sync).
"""
from __future__ import annotations

from ._base import _WRITE_LOCK, _log

_LISTS = ("facts", "learnings", "key_results")


def _normalize_brief(path) -> bool:
    from aiforge_core.runtime import work_notes
    text = path.read_text(encoding="utf-8", errors="replace")
    sec = work_notes.parse_note(text)["sections"]
    kr, facts, learnings = work_notes.scrub_sections(
        sec.get("key_results"), sec.get("facts"), sec.get("learnings"))
    clean = {"facts": facts, "learnings": learnings, "key_results": kr}
    if all(clean[k] == list(sec.get(k) or []) for k in _LISTS) and "\n- - " not in text:
        return False
    return bool(work_notes.update_note(str(path), **clean).get("ok"))


def _normalize_okf_nodes() -> int:
    from aiforge_core.memory.okf import store
    from aiforge_core.memory.okf._author_topics import _fact_lines
    fixed = 0
    for d in store.load_all():
        if d.get("type") != "learning":
            continue
        body = (d.get("body") or "").strip()
        clean = "\n".join(f"- {x}" for x in _fact_lines(body))
        # only bullet-list bodies: prose nodes are left as written
        if not clean or clean == body or not body.startswith("- "):
            continue
        if store.save_node("learning", d["id"], d.get("meta") or {}, clean,
                           reindex=False).get("ok"):
            fixed += 1
    return fixed


def normalize_all() -> dict:
    """Re-render every brief and OKF learning node whose lists are not clean.
    Returns ``{"ok", "briefs", "nodes", "errors"}``; never raises."""
    from . import iter_briefs
    briefs = errors = 0
    with _WRITE_LOCK:
        for p in iter_briefs():
            try:
                briefs += _normalize_brief(p)
            except Exception as exc:  # noqa: BLE001 — one bad file never stops the pass
                errors += 1
                _log.warning("normalize: %s skipped: %s", p.name, exc)
    try:
        nodes = _normalize_okf_nodes()
    except Exception as exc:  # noqa: BLE001
        nodes = 0
        errors += 1
        _log.warning("normalize: okf nodes skipped: %s", exc)
    if briefs or nodes:
        _log.info("normalize: %d brief(s), %d okf node(s) cleaned", briefs, nodes)
    return {"ok": True, "briefs": briefs, "nodes": nodes, "errors": errors}
