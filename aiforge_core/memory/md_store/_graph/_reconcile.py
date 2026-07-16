"""Graph layer — cross-scope dedupe / reconcile / contradiction resolution and
topic merging. Removes redundant/stale/contradictory facts scattered across
scope briefs and folds near-duplicate topic briefs into one. Part of the
``_graph`` package (split from the former flat ``_graph``)."""
from __future__ import annotations

import os

from .._base import (
    _CAPTURE_SIG_RE,
    _WRITE_LOCK,
    _log,
    _slug,
    brief_path,
    iter_briefs,
)
from .._render import _fact_body, _parse_brief, _reconcile_dropped_index, _render_brief


_RECONCILE_SYS = (
    "You clean a set of knowledge-memory facts drawn from several scope briefs "
    "(each line: 'SCOPE :: fact'). Find DUPLICATES (same information, paraphrased "
    "across briefs) and CONTRADICTIONS (one fact supersedes another — a changed "
    "value / status / decision). For every REDUNDANT or STALE fact, output an "
    "item {scope, fact} to REMOVE, keeping the single best/newest version in ONE "
    "scope (prefer the broadest: shared > a topic > a project). Copy the fact "
    "text VERBATIM as given. Only remove genuine redundancy/contradiction — when "
    "unsure, keep it. Most facts are unique and stay."
)


def reconcile_briefs(*, role: str = "learner", max_facts: int = 400) -> dict:
    """CROSS-brief semantic cleanup: an LLM finds duplicate/contradictory facts
    that scattered across different scope briefs (the compaction consolidate only
    dedupes WITHIN a brief) and removes the redundant/stale copies, keeping one
    canonical version in the broadest scope. Feasible only at a bounded fact
    count (skips above ``max_facts`` so it stays a single call). Gated on
    ``AIFORGE_OKR_SCOPE_LLM``; ``AIFORGE_OKR_RECONCILE=0`` disables. Never raises."""
    # OPT-IN (default OFF): an LLM removing facts across briefs unsupervised can
    # be over-aggressive (it dropped ~24% on a stress run) and is inconsistent —
    # too risky for the automatic pipeline. Enable AIFORGE_OKR_RECONCILE=1 to run
    # it (manually or in recompact) when you want an aggressive cross-scope pass.
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0" \
            or os.environ.get("AIFORGE_OKR_RECONCILE", "0") != "1":
        return {"removed": 0, "skipped": "disabled"}
    from aiforge_core.runtime import work_notes
    briefs: dict = {}          # key -> [facts]
    total = 0
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        try:
            facts = work_notes.parse_note(
                p.read_text(encoding="utf-8"))["sections"].get("facts") or []
        except OSError:
            continue
        if facts:
            briefs[key] = facts
            total += len(facts)
    if total < 2 or total > max_facts:
        return {"removed": 0, "skipped": f"facts={total}"}

    listing = "\n".join(f"{k} :: {_fact_body(f)}"
                        for k, fs in briefs.items() for f in fs)
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Rm(BaseModel):
            scope: str = ""
            fact: str = ""

        class _Removes(BaseModel):
            removes: list[_Rm] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _RECONCILE_SYS},
             {"role": "user", "content": listing[:24000]}],
            _Removes, max_tokens=2000, max_retries=1, temperature=0.0)
        removes = getattr(res, "removes", None) or []
    except Exception as exc:  # noqa: BLE001
        _log.debug("reconcile_briefs LLM failed: %s", exc)
        return {"removed": 0, "error": "llm_unreachable"}

    # group removals by scope key → ci-keys to drop
    drop: dict = {}
    for r in removes:
        k = (getattr(r, "scope", "") or "").strip()
        f = (getattr(r, "fact", "") or "").strip()
        if k in briefs and f:
            drop.setdefault(k, set()).add(work_notes._ci_key(f))
    removed = 0
    with _WRITE_LOCK:
        for k, dks in drop.items():
            p = brief_path(k)
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in dks]
            if len(kept) != len(facts):
                removed += len(facts) - len(kept)
                work_notes.update_note(str(p), facts=kept, kind="knowledge", key=k)
    return {"removed": removed, "scopes": len(drop)}


_CONTRADICT_SYS = (
    "You are given knowledge facts from several scope briefs — each line is "
    "'SCOPE :: fact' (SCOPE is a project name, a topic, or 'shared' = global). "
    "Recall UNIONS a project's brief with the global 'shared' brief, so a fact "
    "in one scope that CONTRADICTS a fact in another is surfaced together and "
    "misleads. Find ONLY DIRECT CONTRADICTIONS: two facts about the SAME specific "
    "subject asserting MUTUALLY EXCLUSIVE values — a changed deploy method, port, "
    "runtime/version, status, owner, or decision (e.g. 'deploy via docker' vs "
    "'deploy via systemctl restart'; 'runtime python3.11' vs 'runtime python3.12' "
    "for the SAME thing). For each contradiction, output ONE item {scope, fact} "
    "naming the STALE/outdated fact to REMOVE, keeping the current one.\n"
    "STRICT RULES: (1) ONLY genuine contradictions — NOT duplicates, NOT "
    "paraphrases, NOT merely related facts. (2) Different subjects that share a "
    "word are NOT a contradiction (service A's port vs service B's port; repo X's "
    "runtime vs repo Y's runtime). (3) RECENCY = TRUTH: each scope shows its "
    "'updated' date; when two facts contradict, REMOVE the one from the scope "
    "with the OLDER 'updated' date (it is stale) and keep the newer. If an "
    "explicit correction says 'now X, NOT Y', the 'Y' fact is the stale one. "
    "(4) When in ANY doubt, output NOTHING. Copy the stale fact text VERBATIM. "
    "Most facts have no contradiction — an empty list is the common, correct answer."
)


def resolve_contradictions(*, role: str = "learner", max_facts: int = 400) -> dict:
    """CONTRADICTION-only cross-scope resolver — REPLACE outdated facts.

    A new fact that contradicts what a repo brief OR the global 'shared' brief
    already holds must supersede it (the video's "overwrite outdated facts, don't
    append" rule), because recall unions repo ∪ shared and would otherwise
    surface both. Unlike :func:`reconcile_briefs` (which also removes DUPLICATES
    and was too aggressive → off by default), this pass touches ONLY genuine
    contradictions with a strict prompt, so it is safe to run automatically
    (default ON; ``AIFORGE_OKR_CONTRADICT=0`` disables). Bounded to a single LLM
    call (skips above ``max_facts``). Gated on ``AIFORGE_OKR_SCOPE_LLM``. Never
    raises. Returns ``{removed, scopes}``."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0" \
            or os.environ.get("AIFORGE_OKR_CONTRADICT", "1") == "0":
        return {"removed": 0, "skipped": "disabled"}
    from aiforge_core.runtime import work_notes
    briefs: dict = {}          # key -> [facts]
    updated: dict = {}         # key -> updated_at (recency tiebreaker)
    total = 0
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            facts = parsed["sections"].get("facts") or []
        except OSError:
            continue
        if facts:
            briefs[key] = facts
            updated[key] = (parsed.get("frontmatter") or {}).get("updated_at") or ""
            total += len(facts)
    if total < 2 or total > max_facts:
        return {"removed": 0, "skipped": f"facts={total}"}

    # scope label carries the brief's updated date so the model picks the STALE
    # (older) side of a contradiction — recency = truth.
    listing = "\n".join(f"{k} (updated {updated.get(k) or '?'}) :: {_fact_body(f)}"
                        for k, fs in briefs.items() for f in fs)
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Rm(BaseModel):
            scope: str = ""
            fact: str = ""

        class _Removes(BaseModel):
            removes: list[_Rm] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _CONTRADICT_SYS},
             {"role": "user", "content": listing[:24000]}],
            _Removes, max_tokens=2000, max_retries=1, temperature=0.0)
        removes = getattr(res, "removes", None) or []
    except Exception as exc:  # noqa: BLE001
        _log.debug("resolve_contradictions LLM failed: %s", exc)
        return {"removed": 0, "error": "llm_unreachable"}

    drop: dict = {}
    for r in removes:
        k = (getattr(r, "scope", "") or "").strip()
        f = (getattr(r, "fact", "") or "").strip()
        if k in briefs and f:
            drop.setdefault(k, set()).add(work_notes._ci_key(f))
    removed = 0
    with _WRITE_LOCK:
        for k, dks in drop.items():
            p = brief_path(k)
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in dks]
            if len(kept) != len(facts):
                # reconcile the search index too, so the stale contradicted fact
                # stops surfacing before the next full reingest.
                _reconcile_dropped_index([f for f in facts if f not in kept], k)
                removed += len(facts) - len(kept)
                work_notes.update_note(str(p), facts=kept, kind="knowledge", key=k)
    return {"removed": removed, "scopes": len(drop)}


def dedupe_global_copies() -> dict:
    """Remove facts from project/topic briefs when the SAME fact (case-insensitive)
    already lives in the global ``compacted-shared.md`` brief. Recall always
    unions the global brief for every scope, so those copies are pure redundancy
    — dropping them de-duplicates the md layer without any recall loss. Fresh
    read-modify-write under ``_WRITE_LOCK``. Never raises."""
    from aiforge_core.runtime import work_notes
    shared = brief_path("shared")
    if not shared.is_file():
        return {"removed": 0, "briefs": 0}
    try:
        gfacts = work_notes.parse_note(
            shared.read_text(encoding="utf-8"))["sections"].get("facts") or []
    except OSError:
        return {"removed": 0, "briefs": 0}
    gkeys = {work_notes._ci_key(_fact_body(f)) for f in gfacts}
    if not gkeys:
        return {"removed": 0, "briefs": 0}
    removed = 0
    touched = 0
    with _WRITE_LOCK:
        for p in iter_briefs():
            if p.name == "compacted-shared.md" or _CAPTURE_SIG_RE.search(p.name):
                continue
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in gkeys]
            if len(kept) != len(facts):
                removed += len(facts) - len(kept)
                touched += 1
                work_notes.update_note(str(p), facts=kept, kind="knowledge",
                                       key=p.stem[len("compacted-"):])
    return {"removed": removed, "briefs": touched}


def _topic_clusters(keys: list[str]) -> list[list[str]]:
    """Group topic keys that are the SAME subject: prefix-family (one extends
    another at a word boundary — gpsd / gpsd-config / gpsd-configuration) OR
    fuzzy-near-identical (note / notes). Union-find over both signals. Returns
    only clusters with >1 member (the ones worth merging)."""
    import difflib
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # canonical root = the SHORTER (broader) name
            if len(rb) < len(ra) or (len(rb) == len(ra) and rb < ra):
                ra, rb = rb, ra
            parent[rb] = ra

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if (a.startswith(b + "-") or b.startswith(a + "-")   # prefix family
                    or a == b + "s" or b == a + "s"              # plural (note/notes)
                    or difflib.SequenceMatcher(None, a, b).ratio() >= 0.9):
                union(a, b)
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return [sorted(v, key=len) for v in groups.values() if len(v) > 1]


def merge_similar_topics() -> dict:
    """Consolidate near-duplicate TOPIC briefs into ONE — kills the
    ``gpsd`` / ``gpsd-config`` / ``gpsd-configuration`` (and ``note`` / ``notes``)
    sprawl that made the Memory page a junk drawer. For each cluster the SHORTER
    (broader) name is canonical; the others' Facts / Learnings / Links / Key
    Results are unioned into it and the duplicate briefs deleted (index rows
    reconciled). Deterministic (no LLM). Repo briefs (discovered repo names) and
    the global ``shared`` brief are PROTECTED from merging. Default on
    (``AIFORGE_OKR_TOPIC_MERGE``); never raises. Returns ``{merged, groups}``."""
    if os.environ.get("AIFORGE_OKR_TOPIC_MERGE", "1") == "0":
        return {"merged": 0, "skipped": "disabled"}
    from aiforge_core.runtime import work_notes
    # protect repo briefs: a discovered repo's brief must never fold into another
    protected = {"shared"}
    try:
        from aiforge_core.memory.migrations import _discover_repos
        protected |= {_slug(r) for r in (_discover_repos() or [])}
    except Exception:  # noqa: BLE001
        pass
    keys = [p.stem[len("compacted-"):] for p in iter_briefs()
            if not _CAPTURE_SIG_RE.search(p.name)
            and p.stem[len("compacted-"):] not in protected]
    clusters = _topic_clusters(keys)
    merged = 0
    done: list[list[str]] = []
    with _WRITE_LOCK:
        for cluster in clusters:
            canonical = cluster[0]
            cpath = brief_path(canonical)
            if not cpath.exists():
                continue
            try:
                cb = _parse_brief(cpath.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = list(cb["facts"]); learns = list(cb["learnings"])
            links = list(cb.get("links") or []); krs = list(cb["key_results"])
            body = cb["body"]; title = cb["title"]
            moved_any = False
            for other in cluster[1:]:
                op = brief_path(other)
                if not op.exists():
                    continue
                try:
                    ob = _parse_brief(op.read_text(encoding="utf-8"))
                except OSError:
                    continue
                for f in ob["facts"]:
                    if f not in facts and _fact_body(f) not in {_fact_body(x) for x in facts}:
                        facts.append(f)
                for l in ob["learnings"]:
                    if l not in learns:
                        learns.append(l)
                for lk in (ob.get("links") or []):
                    if lk not in links:
                        links.append(lk)
                for kr in ob["key_results"]:
                    if kr not in krs:
                        krs.append(kr)
                if ob["body"] and ob["body"] not in body:
                    body = (body + "\n\n" + ob["body"]).strip() if body else ob["body"]
                # remove the duplicate brief + its index rows
                _reconcile_dropped_index(ob["facts"], other)
                try:
                    from aiforge_core.memory import backend_select, sqlite_memory
                    if backend_select.embedded():
                        sqlite_memory.delete_by_source(f"compacted:compacted-{other}")
                        sqlite_memory.delete_by_source(f"md:compacted-{other}")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    op.unlink()
                except OSError:
                    pass
                moved_any = True
                merged += 1
            if moved_any:
                cpath.write_text(
                    _render_brief(canonical, facts=facts, body_md=body,
                                  learnings=learns, title=title, key_results=krs,
                                  links=links),
                    encoding="utf-8")
                done.append(cluster)
    return {"merged": merged, "groups": done}
