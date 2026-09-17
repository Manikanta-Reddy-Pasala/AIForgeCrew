"""Graph layer — cross-scope dedupe / reconcile / contradiction resolution and
topic merging. Removes redundant/stale/contradictory facts scattered across
scope briefs and folds near-duplicate topic briefs into one. Part of the
``_graph`` package (split from the former flat ``_graph``)."""
from __future__ import annotations

import os
import re

from .._base import (
    _CAPTURE_SIG_RE,
    _WRITE_LOCK,
    _log,
    _slug,
    brief_path,
    iter_briefs,
)
from .._render import _fact_body, _parse_brief, _reconcile_dropped_index, _render_brief
from ._reconcile_merge import (  # noqa: F401  # re-exported
    _EMPTY_BRIEF,
    _absorb,
    _archive_file,
    _canonical_name,
    _drop_brief,
    _fold_kind_briefs,
    _load_brief,
    _merge_cluster,
    _mergeable_topic_keys,
    _mintable,
    _protected_topics,
    _same_subject,
    _same_topic_family,
    _topic_clusters,
    _uf_find,
    _uf_union,
    _write_brief_file,
    fold_kind_briefs,
    merge_similar_topics,
    tidy_briefs,
)
from ._reconcile_names import (  # noqa: F401  # re-exported
    _common_token_prefix,
    _lev1,
    _merge_families,
    _shared_prefix,
    _toks,
    _topic_merge_ratio,
    _transpose1,
    _typo_sibling,
)

# Brief stems that are a note's KIND, not a topic — these were minted by the old
# kind-fallback in _group_key and are junk (compacted-learning.md etc.). tidy_
# briefs folds them into the global shared brief.
_KIND_BRIEF_STEMS = frozenset({
    "learning", "topic-learning", "user-comment", "rule", "session", "note",
    "project-learning", "topic-suggestion", "skills", "task-history",
    "project", "repo",
})


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


def _collect_brief_facts() -> tuple[dict, dict, int]:
    """``(facts_by_scope, updated_by_scope, total)`` across every brief."""
    from aiforge_core.runtime import work_notes
    briefs: dict = {}
    updated: dict = {}         # key -> updated_at (recency tiebreaker)
    total = 0
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        facts = parsed["sections"].get("facts") or []
        if not facts:
            continue
        key = p.stem[len("compacted-"):]
        briefs[key] = facts
        updated[key] = (parsed.get("frontmatter") or {}).get("updated_at") or ""
        total += len(facts)
    return briefs, updated, total


def _ask_for_removals(role: str, system_prompt: str, listing: str) -> list | None:
    """One bounded structured call asking which facts to drop. None on failure."""
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
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": listing[:24000]}],
            _Removes, max_tokens=2000, max_retries=1, temperature=0.0)
        return getattr(res, "removes", None) or []
    except Exception as exc:  # noqa: BLE001
        _log.debug("brief-removal LLM failed: %s", exc)
        return None


def _removals_by_scope(removes: list, briefs: dict) -> dict:
    """``{scope: {ci-key, …}}`` for the removals that name a scope we hold."""
    from aiforge_core.runtime import work_notes
    drop: dict = {}
    for r in removes:
        k = (getattr(r, "scope", "") or "").strip()
        f = (getattr(r, "fact", "") or "").strip()
        if k in briefs and f:
            drop.setdefault(k, set()).add(work_notes._ci_key(f))
    return drop


def _apply_removals(drop: dict, *, reindex: bool) -> int:
    """Rewrite each brief without its dropped facts; returns how many went."""
    from aiforge_core.runtime import work_notes
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
            if len(kept) == len(facts):
                continue
            if reindex:
                # reconcile the search index too, so the stale contradicted fact
                # stops surfacing before the next full reingest.
                _reconcile_dropped_index([f for f in facts if f not in kept], k)
            removed += len(facts) - len(kept)
            work_notes.update_note(str(p), facts=kept, kind="knowledge", key=k)
    return removed


def _llm_fact_removal_pass(*, role: str, max_facts: int, system_prompt: str,
                           label: bool, reindex: bool) -> dict:
    """The shared body of the two cross-brief removal passes: collect facts,
    ask ONE model call which to drop, apply. ``label`` puts each scope's updated
    date in the listing (the recency tiebreaker the contradiction prompt needs)."""
    briefs, updated, total = _collect_brief_facts()
    if total < 2 or total > max_facts:
        return {"removed": 0, "skipped": f"facts={total}"}
    if label:
        listing = "\n".join(
            f"{k} (updated {updated.get(k) or '?'}) :: {_fact_body(f)}"
            for k, fs in briefs.items() for f in fs)
    else:
        listing = "\n".join(f"{k} :: {_fact_body(f)}"
                            for k, fs in briefs.items() for f in fs)
    removes = _ask_for_removals(role, system_prompt, listing)
    if removes is None:
        return {"removed": 0, "error": "llm_unreachable"}
    drop = _removals_by_scope(removes, briefs)
    return {"removed": _apply_removals(drop, reindex=reindex),
            "scopes": len(drop)}


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
    return _llm_fact_removal_pass(role=role, max_facts=max_facts,
                                  system_prompt=_RECONCILE_SYS,
                                  label=False, reindex=False)


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
    return _llm_fact_removal_pass(role=role, max_facts=max_facts,
                                  system_prompt=_CONTRADICT_SYS,
                                  label=True, reindex=True)


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
