"""Reclassifying global learnings: moving repo-specific ones to their repo and
trashing noise."""
from __future__ import annotations

from . import store as _store


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.okf.author as package
    return package


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
        # Unquoted: the quoted form hid the only reference to _Decision.
        # PEP 563 stringifies it again before pydantic resolves it.
        decisions: list[_Decision] = []

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
    category/body (token 6+ chars, so a 5-letter
    word like 'Cache' alone won't false-hit), that is
    the owner — generic name matching, no hardcoded service→repo table."""
    m = node.get("meta") or {}
    hay = (str(m.get("category") or "") + " "
           + (node.get("body") or "")).lower().replace("-", "")
    best = ""
    for rp in repo_set:
        key = rp.lower().replace("-", "")
        if len(key) > 5 and key in hay and len(key) > len(best):
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
        decisions = _pkg()._reclassify_decisions(items, ", ".join(sorted(repo_set)))
    except Exception as exc:  # noqa: BLE001 — no model / bad import
        return {"ok": False, "error": f"import: {exc}"}

    by_id = {d.get("id"): d for d in glob}
    decided = {d.id: d for d in decisions if d.id in by_id}
    plan = _reclassify_plan(by_id, decided, repo_set)
    if dry_run:
        return {"ok": True, "dry_run": True, "move": plan["move"],
                "delete": plan["delete"], "keep": len(plan["keep"])}
    moved = _apply_moves(plan["move"], by_id)
    deleted = _pkg()._trash_noise(plan["delete"], by_id)
    _store._invalidate()       # nodes moved to .trash → drop stale parse cache
    _store._write_index()
    return {"ok": True, "moved": moved, "deleted_to_trash": deleted,
            "kept": len(plan["keep"]), "scopes": _store.okr_scopes()}
