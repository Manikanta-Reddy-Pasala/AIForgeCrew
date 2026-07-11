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
                                 o.context.strip(), reindex=False)
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
        r = _store.save_node("key_result", None, meta, "", reindex=False)
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
        r = _store.save_node("learning", None, meta, ln.rule.strip(),
                             reindex=False)
        made["learnings"].append(r.get("id"))
    if made["objectives"] or made["key_results"] or made["learnings"]:
        _store._write_index()          # one index rewrite for the whole batch
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
    repo_list = ", ".join(sorted(repo_set))
    # Small batches: a local model reasons far better over ~8 items than 40 —
    # a big JSON blob made it skip every project mapping. Chunk + accumulate.
    all_dec = []
    for i in range(0, len(items), 8):
        batch = items[i:i + 8]
        try:
            res = structured_complete(
                "learner",
                [{"role": "system", "content": _RECLASSIFY_SYS},
                 {"role": "user", "content":
                     "REPOS: " + repo_list + "\n\n"
                     "For each learning below, if its text names a class / "
                     "service / package / module, MAP it to the repo that owns "
                     "that name (match by name similarity to a repo, e.g. a "
                     "'CacheLayer' fact → CacheLayer, a 'SagaTransaction'/saga "
                     "fact → the server backend, a 'ChartOfAccounts' cache → the "
                     "cache repo). Only 'noise' for scratch/test-session junk.\n\n"
                     "LEARNINGS:\n" + _json.dumps(batch, ensure_ascii=False)}],
                _Out, max_retries=1, max_tokens=1200, temperature=0.0)
            all_dec.extend(res.decisions)
        except Exception:  # noqa: BLE001 — a bad batch just keeps those global
            continue

    by_id = {d.get("id"): d for d in glob}
    plan = {"move": [], "delete": [], "keep": []}
    decided = {}
    for dec in all_dec:
        if dec.id in by_id:
            decided[dec.id] = dec
    # DETERMINISTIC repo-name assist: a local model reliably marks noise but
    # rarely maps to a repo. For anything it left global, if a repo NAME appears
    # verbatim in the learning's category/body (token ≥5 chars, so 'Cache' alone
    # won't false-hit), move it there — generic name matching, no hardcoded
    # service→repo table.
    def _name_match(node) -> str:
        m = node.get("meta") or {}
        hay = (str(m.get("category") or "") + " "
               + (node.get("body") or "")).lower().replace("-", "")
        best = ""
        for rp in repo_set:
            key = rp.lower().replace("-", "")
            if len(key) >= 5 and key in hay and len(key) > len(best):
                best = rp
        return best
    for nid, node in by_id.items():
        dec = decided.get(nid)
        if dec and dec.decision == "noise":
            plan["delete"].append(nid)
            continue
        if dec and dec.decision == "project" and dec.repo.strip() in repo_set:
            plan["move"].append((nid, dec.repo.strip()))
            continue
        hit = _name_match(node)                # LLM said global/none → try name
        if hit:
            plan["move"].append((nid, hit))
        else:
            plan["keep"].append(nid)
    if dry_run:
        return {"ok": True, "dry_run": True,
                "move": plan["move"], "delete": plan["delete"],
                "keep": len(plan["keep"])}

    import os as _os
    import shutil as _sh
    moved = deleted = 0
    for nid, repo in plan["move"]:
        node = by_id[nid]
        meta = dict(node.get("meta") or {})
        meta["scope"] = f"repo:{repo}"
        meta["workspace"] = repo               # → projects/<repo>/ via _scope_of
        r = _store.save_node("learning", nid, meta, node.get("body") or "",
                             reindex=False)
        if r.get("ok"):
            moved += 1
    # REVERSIBLE delete: noise nodes MOVE to okr/.trash/ (not unlink) so a
    # mis-classified learning can be restored.
    trash = _os.path.join(_store.okr_root(), ".trash")
    for nid in plan["delete"]:
        with __import__("contextlib").suppress(OSError):
            _os.makedirs(trash, exist_ok=True)
            _sh.move(by_id[nid]["path"], _os.path.join(trash, f"{nid}.md"))
            deleted += 1
    _store._invalidate()       # nodes moved to .trash → drop stale parse cache
    _store._write_index()
    return {"ok": True, "moved": moved, "deleted_to_trash": deleted,
            "kept": len(plan["keep"]), "scopes": _store.okr_scopes()}


def record_repo_profile(workspace: str, *, stack: str = "", build: str = "",
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
        for k, v in (("stack", stack), ("build", build), ("test", test),
                     ("run", run), ("structure", structure), ("deploy", deploy)):
            if v and str(v).strip():
                meta[k] = str(v).strip()

        def _union(key, new):
            cur = list(meta.get(key) or [])
            for x in (new or []):
                x = str(x).strip()
                if x and x not in cur:
                    cur.append(x)
            if cur:
                meta[key] = cur[:30]
        _union("entry_points", entry_points)
        _union("services", services)
        _union("tables", tables)
        _union("gotchas", gotchas)
        _union("conventions", conventions)
        _union("scripts", scripts)
        _union("workflows", workflows)
        if date:
            meta["timestamp"] = date
        newbody = (body or "").strip() or (existing or {}).get("body") or ""
        return _store.save_node("repo", nid, meta, newbody)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


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
        for d in _store.load_all(workspace or None):
            if d.get("type") == "script":
                m = d.get("meta") or {}
                if m.get("name") == name and (m.get("workspace") or "") == (workspace or ""):
                    return {"ok": True, "id": d.get("id"), "deduped": True}
        meta: dict = {"name": name, "lang": lang,
                      "title": f"{name} ({lang})"}
        if purpose:
            meta["purpose"] = purpose.strip()[:200]
        if path:
            meta["path"] = path
        if run:
            meta["run"] = run
        if workspace:
            meta["workspace"] = workspace
            meta["scope"] = f"repo:{workspace}"
        meta["about"] = list(about or [])
        if date:
            meta["timestamp"] = date
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
        for d in _store.load_all(workspace or None):
            if d.get("type") == "task":
                m = d.get("meta") or {}
                if _dedup_key(m.get("title") or "") == key \
                        and (m.get("workspace") or "") == (workspace or ""):
                    return {"ok": True, "id": d.get("id"), "deduped": True}
        meta: dict = {"title": title}
        if workspace:
            meta["workspace"] = workspace
            meta["scope"] = f"repo:{workspace}"
        meta["about"] = list(about or [])
        if tags:
            meta["tags"] = list(tags)
        if date:
            meta["timestamp"] = date
        return _store.save_node("task", None, meta, body or title)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


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
        buckets: dict[str, list[str]] = {}
        for d in learns:
            cat = str((d.get("meta") or {}).get("category") or "notes").lower()
            buckets.setdefault(cat, []).append(
                (d.get("body") or "").strip().lstrip("- ").strip())

        def _first(cats):
            for c in cats:
                for cat, facts in buckets.items():
                    if c in cat and facts:
                        return facts[0][:200]
            return ""
        build = _first(["build", "ci-cd", "compile"])
        test = _first(["test", "mocking"])
        structure = _first(["structure", "architecture", "layout"])
        gotchas = [f for facts in buckets.values() for f in facts if f][:12]
        r = record_repo_profile(
            ws, build=build, test=test, structure=structure, gotchas=gotchas,
            body="Auto-built from this repo's learnings; refine as you work.")
        if r.get("ok"):
            made += 1
    return {"ok": True, "profiles": made}


def migrate_from_briefs() -> dict:
    """Seed the OKR graph from the existing flat briefs: each compacted-<key>.md
    (+ its split parts) → one GLOBAL Learning node (category=<topic>, body = the
    brief's Facts). Scoping to a project is NOT done here — deterministic tag/key
    parsing can't tell a repo brief from a topic brief and produces casing splits
    (AIForgeCrew vs aiforgecrew) and bogus projects (session ids). The migration
    chain's LLM ``classify`` step (which has the real repo list) sorts these
    global learnings into projects/noise afterwards, with consistent casing.
    Idempotent — a category already migrated is skipped. Soft-fail."""
    import re

    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes
    g = _graph.build(force=True)
    have = {str((n.get("meta") or {}).get("category") or "").lower()
            for n in g.nodes.values() if n.get("type") == "learning"}
    facts_by_topic: dict[str, list[str]] = {}
    for p in md_store.memory_dir().glob("compacted-*.md"):
        base = p.stem[len("compacted-"):]
        topic = re.sub(r"-\d+$", "", base)
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if (parsed["frontmatter"] or {}).get("kind") != "knowledge":
            continue
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
                             body, reindex=False)
        if r.get("ok"):
            made += 1
    if made:
        _store._write_index()          # one rewrite for the whole migration
    return {"ok": True, "migrated": made, "topics": len(facts_by_topic)}


__all__ = ["extract_and_save", "write_session_node", "migrate_from_briefs",
           "record_solution", "reclassify_global_learnings",
           "record_repo_profile", "record_script", "record_task",
           "build_repo_profiles"]
