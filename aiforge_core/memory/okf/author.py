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

_log = logging.getLogger("aiforge.okf")


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


def _existing_solution(ticket: str, kind: str, norm: str) -> dict | None:
    """A solution node already recorded for this work.

    DEDUP: never write a second solution node for the same fix. Matches on
    (ticket + kind) or a normalized summary already recorded — so re-runs of
    the learner on the same work don't pile up duplicate S-NN nodes.
    """
    for d in _store.load_all():
        if d.get("type") != "solution":
            continue
        m = d.get("meta") or {}
        by_ticket = ticket and m.get("ticket") == ticket and m.get("kind") == kind
        by_summary = norm and _dedup_key(
            m.get("description") or m.get("title") or "") == norm
        if by_ticket or by_summary:
            return d
    return None


def _clean_list(values, limit: int | None = None) -> list[str]:
    out = [str(v).strip() for v in (values or []) if str(v).strip()]
    return out[:limit] if limit else out


def _solution_meta(*, kind: str, title: str, summary: str, workspace: str,
                   topic: str, tables, services, files, about, ticket: str,
                   date: str) -> dict:
    meta: dict = {"kind": kind, "title": title,
                  "description": (summary or "").strip()[:200]}
    if workspace:
        meta["workspace"] = workspace
        meta["resource"] = f"repo:{workspace}"     # OKF `resource` URI
    for key, value in (("topic", topic), ("ticket", ticket),
                       ("timestamp", date)):
        if value:
            meta[key] = value
    for key, values, limit in (("tables", tables, None),
                               ("services", services, None),
                               ("files", files, 20)):
        cleaned = _clean_list(values, limit)
        if cleaned:
            meta[key] = cleaned
    # about → OKF links (the symbols/paths/tickets this solution relates to)
    meta["about"] = list(about or [])
    return meta


def _append_solution_log(*, kind: str, title: str, node_id, workspace: str,
                         tables, services, date: str) -> None:
    """The dated audit trail (reserved OKF log.md, newest-first)."""
    try:
        import os as _os

        from aiforge_core.memory import okf
        extra = []
        if workspace:
            extra.append(f"workspace:{workspace}")
        if tables:
            extra.append(f"tables:{','.join(tables[:6])}")
        if services:
            extra.append(f"services:{','.join(services[:6])}")
        entry = (f"[{kind}] {title}"
                 + (f" ({'; '.join(extra)})" if extra else "")
                 + f" · {node_id}")
        okf.append_log(_os.path.join(_store.okf_root(), "log.md"),
                       entry, date=date)
    except Exception:  # noqa: BLE001 — log is best-effort
        pass


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
        dup = _existing_solution(ticket, kind, _dedup_key(summary))
        if dup is not None:
            return {"ok": True, "id": dup.get("id"), "path": dup.get("path"),
                    "deduped": True}
        meta = _solution_meta(kind=kind, title=title, summary=summary,
                              workspace=workspace, topic=topic, tables=tables,
                              services=services, files=files, about=about,
                              ticket=ticket, date=date)
        r = _store.save_node("solution", None, meta,
                             body or (summary or "").strip())
        if date and r.get("ok"):
            _append_solution_log(kind=kind, title=title, node_id=r.get("id"),
                                 workspace=workspace, tables=tables,
                                 services=services, date=date)
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


_RECLASSIFY_USER_PREAMBLE = (
    "For each learning below, if its text names a class / service / package / "
    "module, MAP it to the repo that owns that name (match by name similarity "
    "to a repo, e.g. a 'CacheLayer' fact → CacheLayer, a 'SagaTransaction'/saga "
    "fact → the server backend, a 'ChartOfAccounts' cache → the cache repo). "
    "Only 'noise' for scratch/test-session junk.\n\nLEARNINGS:\n")


def _reclassify_decisions(items: list[dict], repo_list: str) -> list:
    """Ask the model to triage the catalogue, in SMALL batches.

    A local model reasons far better over ~8 items than 40 — a big JSON blob
    made it skip every project mapping. A bad batch just leaves those global.
    """
    import json as _json

    from pydantic import BaseModel

    from aiforge_core.llm.structured import structured_complete

    class _Decision(BaseModel):
        id: str
        decision: str = "global"       # global | project | noise
        repo: str = ""

    class _Out(BaseModel):
        decisions: "list[_Decision]" = []

    out = []
    for i in range(0, len(items), 8):
        try:
            res = structured_complete(
                "learner",
                [{"role": "system", "content": _RECLASSIFY_SYS},
                 {"role": "user", "content":
                     "REPOS: " + repo_list + "\n\n" + _RECLASSIFY_USER_PREAMBLE
                     + _json.dumps(items[i:i + 8], ensure_ascii=False)}],
                _Out, max_retries=1, max_tokens=1200, temperature=0.0)
            out.extend(res.decisions)
        except Exception:  # noqa: BLE001
            continue
    return out


def _repo_name_match(node: dict, repo_set: set) -> str:
    """DETERMINISTIC repo-name assist: a local model reliably marks noise but
    rarely maps to a repo. If a repo NAME appears verbatim in the learning's
    category/body (token ≥5 chars, so 'Cache' alone won't false-hit), that is
    the owner — generic name matching, no hardcoded service→repo table."""
    m = node.get("meta") or {}
    hay = (str(m.get("category") or "") + " "
           + (node.get("body") or "")).lower().replace("-", "")
    best = ""
    for rp in repo_set:
        key = rp.lower().replace("-", "")
        if len(key) >= 5 and key in hay and len(key) > len(best):
            best = rp
    return best


def _reclassify_plan(by_id: dict, decided: dict, repo_set: set) -> dict:
    plan: dict = {"move": [], "delete": [], "keep": []}
    for nid, node in by_id.items():
        dec = decided.get(nid)
        if dec and dec.decision == "noise":
            plan["delete"].append(nid)
            continue
        if dec and dec.decision == "project" and dec.repo.strip() in repo_set:
            plan["move"].append((nid, dec.repo.strip()))
            continue
        hit = _repo_name_match(node, repo_set)  # LLM said global/none → try name
        if hit:
            plan["move"].append((nid, hit))
        else:
            plan["keep"].append(nid)
    return plan


def _apply_moves(moves: list, by_id: dict) -> int:
    moved = 0
    for nid, repo in moves:
        node = by_id[nid]
        meta = dict(node.get("meta") or {})
        meta["scope"] = f"repo:{repo}"
        meta["workspace"] = repo               # → projects/<repo>/ via _scope_of
        if _store.save_node("learning", nid, meta, node.get("body") or "",
                            reindex=False).get("ok"):
            moved += 1
    return moved


def _trash_noise(ids: list, by_id: dict) -> int:
    """LOCALLY REVERSIBLE delete: noise nodes MOVE to okf/.trash/ (not unlink)
    so a mis-classified learning can be restored *on this machine* — put the
    file back and retrieval sees it again. It does NOT come back mesh-wide: the
    tombstone travels at rev+1, so the restored file (still at the old rev) is
    no longer the advertised version of its identity. Re-publishing a restored
    node means re-authoring it, which stamps a fresh rev. ``.trash`` is a
    dot-directory, so ``_io.iter_syncable`` never advertises, serves or folds
    what lands there."""
    import contextlib as _cl
    import os as _os
    import shutil as _sh

    from aiforge_core.memory.sync import tombstone as _tomb
    trash = _os.path.join(_store.okf_root(), ".trash")
    deleted = 0
    for nid in ids:
        meta = by_id[nid].get("meta") or {}
        with _cl.suppress(OSError):
            _os.makedirs(trash, exist_ok=True)
            _sh.move(by_id[nid]["path"], _os.path.join(trash, f"{nid}.md"))
            deleted += 1
            # Removal has to be expressible to the mesh: without this the next
            # pull from any peer re-plants the node we just called noise.
            _tomb.mark_deleted(meta.get("origin"), nid, meta.get("rev"))
    return deleted


def reclassify_global_learnings(repos: "list[str]", *, dry_run: bool = False) -> dict:
    """Triage the learnings currently in ``global/``: an LLM decides each is a
    real GLOBAL rule (keep), PROJECT-specific (→ move to projects/<repo>/ by
    setting workspace), or NOISE (a transient test-session artifact → delete).
    ``repos`` is the known-repo whitelist the classifier maps to. ``dry_run``
    returns the plan without touching disk. Soft-fail; never raises."""
    glob = [d for d in _store.load_all("global") if d.get("type") == "learning"]
    if not glob:
        return {"ok": True, "moved": 0, "deleted": 0, "kept": 0,
                "note": "no global learnings"}
    repo_set = {r.strip() for r in repos if r.strip()}
    # compact catalogue for the model: id · category · first line
    items = [{"id": d.get("id"),
              "category": (d.get("meta") or {}).get("category") or "",
              "text": (d.get("body") or "").strip().split("\n", 1)[0][:160]}
             for d in glob]
    try:
        decisions = _reclassify_decisions(items, ", ".join(sorted(repo_set)))
    except Exception as exc:  # noqa: BLE001 — no model / bad import
        return {"ok": False, "error": f"import: {exc}"}

    by_id = {d.get("id"): d for d in glob}
    decided = {d.id: d for d in decisions if d.id in by_id}
    plan = _reclassify_plan(by_id, decided, repo_set)
    if dry_run:
        return {"ok": True, "dry_run": True, "move": plan["move"],
                "delete": plan["delete"], "keep": len(plan["keep"])}
    moved = _apply_moves(plan["move"], by_id)
    deleted = _trash_noise(plan["delete"], by_id)
    _store._invalidate()       # nodes moved to .trash → drop stale parse cache
    _store._write_index()
    return {"ok": True, "moved": moved, "deleted_to_trash": deleted,
            "kept": len(plan["keep"]), "scopes": _store.okr_scopes()}


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


def _brief_facts_by_topic() -> dict[str, list[str]]:
    """Every knowledge brief's Facts, keyed by topic (split parts folded back)."""
    import re

    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes
    out: dict[str, list[str]] = {}
    for p in md_store.iter_briefs():
        topic = re.sub(r"-\d+$", "", p.stem[len("compacted-"):])
        try:
            parsed = work_notes.parse_note(
                p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if (parsed["frontmatter"] or {}).get("kind") != "knowledge":
            continue
        facts = parsed["sections"].get("facts") or []
        if facts:
            out.setdefault(topic, []).extend(facts)
    return out


def migrate_from_briefs() -> dict:
    """Seed the OKR graph from the existing flat briefs: each compacted-<key>.md
    (+ its split parts) → one GLOBAL Learning node (category=<topic>, body = the
    brief's Facts). Scoping to a project is NOT done here — deterministic tag/key
    parsing can't tell a repo brief from a topic brief and produces casing splits
    (AIForgeCrew vs aiforgecrew) and bogus projects (session ids). The migration
    chain's LLM ``classify`` step (which has the real repo list) sorts these
    global learnings into projects/noise afterwards, with consistent casing.
    Idempotent — a category already migrated is skipped. Soft-fail."""
    g = _graph.build(force=True)
    have = {str((n.get("meta") or {}).get("category") or "").lower()
            for n in g.nodes.values() if n.get("type") == "learning"}
    facts_by_topic = _brief_facts_by_topic()
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


def _brief_facts_by_topic() -> dict[str, list[str]]:
    """Every locally-compacted brief's Facts, grouped by topic. Deterministic —
    no model, no network: it reads the files md_store already wrote."""
    import re

    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes

    out: dict[str, list[str]] = {}
    for p in md_store.iter_briefs():
        base = p.stem[len("compacted-"):]
        topic = re.sub(r"-\d+$", "", base)
        try:
            parsed = work_notes.parse_note(
                p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — one unreadable brief, not a dead pass
            continue
        if (parsed["frontmatter"] or {}).get("kind") != "knowledge":
            continue
        for raw in parsed["sections"].get("facts") or []:
            # Flattened to ONE line. A fact is written back as a single "- …"
            # bullet and read back a line at a time, so a fact containing a
            # newline would never match itself on the next pass: it would look
            # new every cycle, rewrite the node, bump `rev`, and re-trigger the
            # admin's LLM fold — forever.
            fact = " ".join(str(raw).split()).strip()
            if fact and fact not in out.setdefault(topic, []):
                out[topic].append(fact)
    return out


def _fact_lines(body: str) -> list[str]:
    """The bullet lines of a learning node's body, as plain facts.

    Whitespace-normalised the same way ``_brief_facts_by_topic`` normalises what
    it reads out of a brief, so the two halves of "is this fact already held?"
    compare in one form.
    """
    lines = []
    for raw in (body or "").splitlines():
        line = " ".join(raw.split()).strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            lines.append(line)
    return lines


# How much of a topic's fact list one node carries. The cap exists because a
# node is a file an LLM later reads whole; what matters here is that it is
# applied at a LINE boundary. Cutting mid-line left a partial fact in the body,
# which then never matched the whole fact in the brief — so every cycle saw it
# as new, rewrote the node, bumped `rev`, re-pushed it, and re-triggered the
# admin's fold. Non-convergence, forever, on any topic that outgrew the cap.
_BODY_CHARS = 4000


def _body_for(facts: list[str]) -> tuple[str, list[str]]:
    """Render facts as bullets within the cap, and say which ones fitted.

    Returns ``(body, kept)``. Whole lines only — see ``_BODY_CHARS``.
    """
    kept: list[str] = []
    size = 0
    for fact in facts:
        line = f"- {fact}"
        cost = len(line) + (1 if kept else 0)
        if size + cost > _BODY_CHARS:
            break
        kept.append(fact)
        size += cost
    return "\n".join(f"- {f}" for f in kept), kept


def _learning_by_topic(g) -> dict:
    """``{topic: (id, node)}`` for the existing global learning nodes."""
    out: dict = {}
    for nid, node in g.nodes.items():
        if node.get("type") != "learning":
            continue
        cat = str((node.get("meta") or {}).get("category") or "").lower()
        if cat:
            out.setdefault(cat, (nid, node))
    return out


def _create_topic_node(topic: str, facts: list) -> tuple[int, int]:
    """``(created, dropped)`` for a topic no node holds yet."""
    body, kept = _body_for(facts)
    ok = _store.save_node("learning", None,
                          {"scope": "global", "category": topic,
                           "title": f"{topic} knowledge",
                           "tags": [f"topic:{topic}"]},
                          body, reindex=False).get("ok")
    return (1 if ok else 0), len(facts) - len(kept)


def _update_topic_node(topic: str, facts: list, held) -> tuple[int, int]:
    """``(updated, dropped)`` for a topic that already has a node."""
    nid, node = held
    have = _fact_lines(node.get("body") or "")
    fresh = [f for f in facts if f not in have]
    if not fresh:
        return 0, 0       # unchanged: no write, no rev bump, nothing to sync
    body, kept = _body_for(have + fresh)
    dropped = len(have) + len(fresh) - len(kept)
    if _fact_lines(body) == have:
        # The node is full: every fresh fact fell off the end, so writing would
        # produce byte-identical content at a higher rev — and would do so on
        # EVERY cycle. Say so once and leave it alone.
        _log.info("okf: %s knowledge is at the %d-char cap — %d newer "
                  "fact(s) not carried", topic, _BODY_CHARS, len(fresh))
        return 0, dropped
    meta = dict(node.get("meta") or {})
    meta.setdefault("scope", "global")
    meta.setdefault("category", topic)
    ok = _store.save_node("learning", nid, meta, body, reindex=False).get("ok")
    return (1 if ok else 0), dropped


def sync_briefs_to_nodes() -> dict:
    """Turn this machine's compacted briefs into OKF nodes, and keep them current.

    **This is what makes local compaction reach the other machines.** Briefs are
    class A files that stay local by design (each machine compacts its own), so
    the only thing that travels is OKF knowledge — and a fact that never became
    a node never leaves this box. Running this on every cycle closes that gap.

    Deterministic and idempotent: one global learning node per brief topic,
    body = the union of that topic's facts, newest appended. A topic already
    represented is UPDATED rather than skipped — the one-shot
    :func:`migrate_from_briefs` skips it, which is right for a migration and
    wrong for a cycle, because every fact added after the first run would be
    invisible forever.

    No LLM is involved: the distillation already happened when the brief was
    written, and re-summarising here would be a second, non-deterministic fold
    of the same text on every machine.
    """
    facts_by_topic = _brief_facts_by_topic()
    if not facts_by_topic:
        return {"ok": True, "created": 0, "updated": 0, "topics": 0}

    existing = _learning_by_topic(_graph.build(force=True))
    created = updated = dropped = 0
    for topic, facts in facts_by_topic.items():
        held = existing.get(topic.lower())
        if held is None:
            n, d = _create_topic_node(topic, facts)
            created += n
        else:
            n, d = _update_topic_node(topic, facts, held)
            updated += n
        dropped += d

    if created or updated:
        _store._write_index()          # one rewrite for the whole pass
    _log.info("okf: briefs → nodes created=%d updated=%d dropped=%d over %d topic(s)",
              created, updated, dropped, len(facts_by_topic))
    return {"ok": True, "created": created, "updated": updated,
            "dropped": dropped, "topics": len(facts_by_topic)}


__all__ = ["extract_and_save", "write_session_node", "migrate_from_briefs",
           "sync_briefs_to_nodes",
           "record_solution", "reclassify_global_learnings",
           "record_repo_profile", "record_script", "record_task",
           "build_repo_profiles"]
