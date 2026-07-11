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


def extract_and_save(session_text: str, *, active_kr: str | None = None,
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
        low = sc.lower()
        meta: dict = {}
        if low == "repo" and repo:
            # project-specific → segregates into projects/<repo>/ (workspace is
            # what store._scope_of keys on).
            meta["scope"] = f"repo:{repo}"
            meta["workspace"] = repo
        elif low in ("global", "") or (low == "repo" and not repo):
            meta["scope"] = "global"
        else:
            oid = title_to_oid.get(_slug(sc)) or _existing_objective_by_title(g, sc)
            meta["scope"] = [oid] if oid else "global"
        if ln.topic.strip():                       # theme axis (orthogonal)
            tp = _topic_slug(ln.topic)
            if tp:
                meta["category"] = tp
                meta["tags"] = [f"topic:{tp}"]
        r = _store.save_node("learning", None, meta, ln.rule.strip())
        made["learnings"].append(r.get("id"))
    made["ok"] = True
    return made


def write_session_node(*, title: str, body: str,
                       linked_krs: list[str] | None = None,
                       repo: str | None = None) -> dict:
    """Write a ``session`` node (chronological log) from a run's steps, linked to
    the KRs it advanced (defaults to the active KR). A ``repo`` scopes the
    session into ``projects/<repo>/`` (it's that repo's activity). Soft-fail."""
    krs = list(linked_krs or [])
    if not krs:
        act = _graph.get_active()
        if act:
            krs = [act]
    import datetime as _dt
    meta = {"date": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d"),
            "linked_krs": krs, "title": title}
    if repo:
        meta["workspace"] = repo
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


_RECLASSIFY_SYS = (
    "You are triaging accumulated GLOBAL learnings in a memory bundle. For EACH "
    "learning decide ONE:\n"
    "• 'project' — it is knowledge SPECIFIC to one repository (its classes, "
    "modules, build/test setup, domain fields, a bug fixed in it). Set `repo` to "
    "the matching name from the provided repo list (best match; the learning's "
    "category or body usually names a class/service/package that belongs to a "
    "repo).\n"
    "• 'global' — a genuinely universal rule that holds across ALL repos (a user "
    "preference, a general convention, a cross-cutting decision).\n"
    "• 'noise' — a transient TEST-SESSION artifact with no durable value: a "
    "one-off status line, a scratch experiment (expense-tracker, httptiny, "
    "user-count, calc, directory listing), 'the model did not respond', an empty "
    "workspace note, a jira ticket status snapshot. These should be DELETED.\n"
    "Only pick 'project' when the repo is a confident match — otherwise 'global' "
    "or 'noise'. Never invent a repo not in the list."
)


def reclassify_global_learnings(repos: "list[str]", *, dry_run: bool = False) -> dict:
    """Triage the learnings currently in ``global/``: an LLM decides each is a
    real GLOBAL rule (keep), PROJECT-specific (→ move to projects/<repo>/ by
    setting workspace), or NOISE (a transient test-session artifact → delete).
    ``repos`` is the known-repo whitelist the classifier maps to. ``dry_run``
    returns the plan without touching disk. Soft-fail; never raises."""
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"import: {exc}"}

    glob = [d for d in _store.load_all("global") if d.get("type") == "learning"]
    if not glob:
        return {"ok": True, "moved": 0, "deleted": 0, "kept": 0, "note": "no global learnings"}
    repo_set = {r.strip() for r in repos if r.strip()}

    # compact catalogue for the model: id · category · first line
    items = []
    for d in glob:
        m = d.get("meta") or {}
        head = (d.get("body") or "").strip().split("\n", 1)[0][:160]
        items.append({"id": d.get("id"), "category": m.get("category") or "",
                      "text": head})

    class _Decision(BaseModel):
        id: str
        decision: str = "global"       # global | project | noise
        repo: str = ""

    class _Out(BaseModel):
        decisions: "list[_Decision]" = []

    import json as _json
    try:
        res = structured_complete(
            "learner",
            [{"role": "system", "content": _RECLASSIFY_SYS},
             {"role": "user", "content":
                 "REPOS:\n" + ", ".join(sorted(repo_set)) + "\n\nLEARNINGS:\n"
                 + _json.dumps(items, ensure_ascii=False)}],
            _Out, max_retries=1, max_tokens=2500, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"llm: {exc}"}

    by_id = {d.get("id"): d for d in glob}
    plan = {"move": [], "delete": [], "keep": []}
    for dec in res.decisions:
        node = by_id.get(dec.id)
        if not node:
            continue
        if dec.decision == "project" and dec.repo.strip() in repo_set:
            plan["move"].append((dec.id, dec.repo.strip()))
        elif dec.decision == "noise":
            plan["delete"].append(dec.id)
        else:
            plan["keep"].append(dec.id)
    # anything the model didn't rule on → keep
    ruled = {i for i, _ in plan["move"]} | set(plan["delete"]) | set(plan["keep"])
    plan["keep"] += [i for i in by_id if i not in ruled]

    if dry_run:
        return {"ok": True, "dry_run": True,
                "move": plan["move"], "delete": plan["delete"],
                "keep": len(plan["keep"])}

    import os as _os
    moved = deleted = 0
    for nid, repo in plan["move"]:
        node = by_id[nid]
        meta = dict(node.get("meta") or {})
        meta["scope"] = f"repo:{repo}"
        meta["workspace"] = repo               # → projects/<repo>/ via _scope_of
        r = _store.save_node("learning", nid, meta, node.get("body") or "")
        if r.get("ok"):
            moved += 1
    for nid in plan["delete"]:
        with __import__("contextlib").suppress(OSError):
            _os.unlink(by_id[nid]["path"])
            deleted += 1
    _store._write_index()
    return {"ok": True, "moved": moved, "deleted": deleted,
            "kept": len(plan["keep"]), "scopes": _store.okr_scopes()}


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


__all__ = ["extract_and_save", "write_session_node", "migrate_from_briefs",
           "record_solution", "reclassify_global_learnings"]
