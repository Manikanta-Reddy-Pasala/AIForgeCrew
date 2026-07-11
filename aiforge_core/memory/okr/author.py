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


def record_solution(*, kind: str, summary: str, workspace: str = "",
                    topic: str = "", tables: "list[str] | None" = None,
                    services: "list[str] | None" = None,
                    files: "list[str] | None" = None,
                    about: "list[str] | None" = None,
                    ticket: str = "", body: str = "", date: str = "") -> dict:
    """Record ONE completed feature or bug fix as an OKF ``solution`` node AND a
    dated ``log.md`` entry — so the OKR bundle is a queryable changelog of what
    was solved, mapped to the workspace/repo it touched, the topic, and the DB
    tables + connected services involved.

    ``kind`` is 'feature' or 'fix'. ``date`` (ISO YYYY-MM-DD) is passed in by the
    caller (no clock here — keeps it reproducible/testable). Soft-fail: never
    raises into the persistence path."""
    try:
        kind = "fix" if str(kind).lower().startswith(("fix", "bug")) else "feature"
        title = (summary or "").strip().split("\n", 1)[0][:90] or f"{kind}"
        # DEDUP: never write a second solution node for the same fix. Match on
        # (ticket + kind) or a normalized summary already recorded — so re-runs
        # of the learner on the same work don't pile up duplicate S-NN nodes.
        _norm = _dedup_key(summary)
        for _d in _store.load_all():
            if _d.get("type") != "solution":
                continue
            _m = _d.get("meta") or {}
            if (ticket and _m.get("ticket") == ticket and _m.get("kind") == kind) \
               or (_norm and _dedup_key(_m.get("description") or _m.get("title")
                                        or "") == _norm):
                return {"ok": True, "id": _d.get("id"), "path": _d.get("path"),
                        "deduped": True}
        meta: dict = {"kind": kind, "title": title,
                      "description": (summary or "").strip()[:200]}
        if workspace:
            meta["workspace"] = workspace
            meta["resource"] = f"repo:{workspace}"     # OKF `resource` URI
        if topic:
            meta["topic"] = topic
        if tables:
            meta["tables"] = [str(t).strip() for t in tables if str(t).strip()]
        if services:
            meta["services"] = [str(s).strip() for s in services if str(s).strip()]
        if files:
            meta["files"] = [str(f).strip() for f in files if str(f).strip()][:20]
        if ticket:
            meta["ticket"] = ticket
        if date:
            meta["timestamp"] = date
        # about → OKF links (the symbols/paths/tickets this solution relates to)
        meta["about"] = list(about or [])
        r = _store.save_node("solution", None, meta, body or (summary or "").strip())
        # dated audit trail (reserved OKF log.md, newest-first)
        if date and r.get("ok"):
            try:
                from aiforge_core.memory import okf
                import os as _os
                extra = []
                if workspace:
                    extra.append(f"workspace:{workspace}")
                if tables:
                    extra.append(f"tables:{','.join(tables[:6])}")
                if services:
                    extra.append(f"services:{','.join(services[:6])}")
                entry = (f"[{kind}] {title}"
                         + (f" ({'; '.join(extra)})" if extra else "")
                         + (f" · {r.get('id')}"))
                okf.append_log(_os.path.join(_store.okr_root(), "log.md"),
                               entry, date=date)
            except Exception:  # noqa: BLE001 — log is best-effort
                pass
        return r
    except Exception as exc:  # noqa: BLE001 — never break the learner path
        return {"ok": False, "error": str(exc)}


def migrate_from_briefs() -> dict:
    """Seed the OKR graph from the existing flat topic briefs: each
    compacted-<topic>.md (+ its split parts) → one global Learning node
    (category=<topic>, body=the topic's Facts). Idempotent — a topic already
    migrated (a learning with that category) is skipped. Briefs are left in
    place. Soft-fail."""
    import re

    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes
    g = _graph.build(force=True)
    have = {str((n.get("meta") or {}).get("category") or "").lower()
            for n in g.nodes.values() if n.get("type") == "learning"}
    # group split parts under their primary topic
    facts_by_topic: dict[str, list[str]] = {}
    for p in md_store.memory_dir().glob("compacted-*.md"):
        base = p.stem[len("compacted-"):]
        topic = re.sub(r"-\d+$", "", base)
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if (parsed["frontmatter"] or {}).get("kind") != "knowledge":
            continue                       # only real topic briefs
        facts = parsed["sections"].get("facts") or []
        if facts:
            facts_by_topic.setdefault(topic, []).extend(facts)
    made = 0
    for topic, facts in facts_by_topic.items():
        if topic.lower() in have or not facts:
            continue
        body = "\n".join(f"- {f}" for f in facts)[:4000]
        r = _store.save_node("learning", None,
                             {"scope": "global", "category": topic,
                              "title": f"{topic} knowledge", "tags": [f"topic:{topic}"]},
                             body)
        if r.get("ok"):
            made += 1
    return {"ok": True, "migrated": made, "topics": len(facts_by_topic)}


__all__ = ["extract_and_save", "write_session_node", "migrate_from_briefs"]
