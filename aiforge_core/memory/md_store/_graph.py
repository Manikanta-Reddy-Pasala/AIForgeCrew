"""md_store internals: the cross-brief graph layer — link mapping / lint /
expansion, scope re-heal, cross-scope dedupe / reconcile / contradiction
resolution and topic merging. The top layer; builds on `_base`, `_render`,
`_scope`, `_capture` and `_compact`."""
from __future__ import annotations

import os

from ._base import (
    _CAPTURE_SIG_RE,
    _COMPACT_LOCK,
    _WRITE_LOCK,
    _brief_title,
    _capture_md_files,
    _log,
    _parse,
    _resolve_md,
    _slug,
    brief_path,
    iter_briefs,
)
from ._capture import capture
from ._compact import sweep_empty_briefs
from ._render import _fact_body, _parse_brief, _reconcile_dropped_index, _render_brief
from ._scope import classify_scope


def lint_graph(*, repair: bool = False) -> dict:
    """Deterministic graph-health lint over the brief link structure (the video's
    periodic linting step). Finds:

    * ``broken`` — a brief's Links entry pointing at a ``compacted-*.md`` file
      that no longer exists (a dangling ref after a brief was deleted/renamed).
    * ``orphans`` — briefs with NO brief-to-brief link either way (candidates for
      the next ``map_scopes`` pass; reported, never deleted — an orphan may be a
      genuinely standalone topic).

    With ``repair=True`` the broken refs are STRIPPED from each brief's Links
    (real URLs / jira refs are kept). Never raises. Returns
    ``{broken, orphans, repaired}``."""
    from aiforge_core.runtime import work_notes
    briefs = [p for p in iter_briefs() if not _CAPTURE_SIG_RE.search(p.name)]
    existing = {p.name for p in briefs}
    linked: set[str] = set()          # keys that have ANY brief-ref (in or out)
    broken: list[dict] = []
    repaired = 0
    parsed_cache: dict = {}
    for p in briefs:
        key = p.stem[len("compacted-"):]
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        parsed_cache[p] = parsed
        for lk in (parsed["sections"].get("links") or []):
            m = work_notes._BRIEF_REF_RE.match(lk.strip())
            if not m:
                continue
            tgt = m.group("file")                     # compacted-<x>.md
            if tgt in existing:
                linked.add(key)
                linked.add(tgt[len("compacted-"):-len(".md")])
            else:
                broken.append({"brief": key, "ref": tgt})
    if repair and broken:
        with _WRITE_LOCK:
            for p in briefs:
                parsed = parsed_cache.get(p)
                if not parsed:
                    continue
                links = list(parsed["sections"].get("links") or [])
                kept = [l for l in links
                        if not (lambda mm: mm and mm.group("file") not in existing)(
                            work_notes._BRIEF_REF_RE.match(l.strip()))]
                if len(kept) != len(links):
                    repaired += len(links) - len(kept)
                    work_notes.update_note(str(p), links=kept, kind="knowledge",
                                           key=p.stem[len("compacted-"):])
    orphans = sorted(p.stem[len("compacted-"):] for p in briefs
                     if p.stem[len("compacted-"):] not in linked)
    return {"broken": broken, "orphans": orphans, "repaired": repaired}
def _live_briefs() -> list[dict]:
    """Canonical scope briefs (``compacted-<scope>.md``), excluding per-run
    capture masqueraders. Each: ``{key, file, path, summary}``."""
    from aiforge_core.runtime import work_notes
    out: list[dict] = []
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        if not key:
            continue
        try:
            d = _parse(p)
            summary = work_notes.knowledge_text(d.get("body") or "")[:200]
        except Exception:  # noqa: BLE001
            continue
        out.append({"key": key, "file": p.name, "path": p, "summary": summary})
    return out


# Directed relationship types + the inverse label written on the OTHER brief, so
# a link reads correctly from both ends (a depends-on b ⇒ b required-by a).
_REL_INVERSE = {
    "depends-on": "required-by",
    "configures": "configured-by",
    "part-of": "has-part",
    "relates-to": "relates-to",
}
_REL_TYPES = tuple(_REL_INVERSE)
_REL_DEFAULT = "relates-to"

_MAP_SYS = (
    "You relate KNOWLEDGE-MEMORY briefs across scopes. Each brief is one scope: "
    "a project (a repo), a cross-cutting topic, or 'shared' (global knowledge).\n"
    "LINK two briefs when they document the SAME SPECIFIC subject, or one "
    "DEPENDS ON / CONFIGURES / IS PART OF the other — such that reading one, you "
    "would want the other. For each link, CLASSIFY the relationship of a → b:\n"
    "  • depends-on — a needs/consumes/reads b (b must exist for a to work)\n"
    "  • configures — a sets up / parameterises b\n"
    "  • part-of — a is a component/subset of b\n"
    "  • relates-to — clearly related, no strong direction\n"
    "Examples that SHOULD link:\n"
    "  • a='time-sync' (chrony consuming gpsd via SHM) → b='gpsd' (the GPS "
    "daemon): depends-on — they document one pipeline at two scopes.\n"
    "  • a=a repo's branch rule → b=the global branch-naming convention: part-of.\n"
    "Do NOT link two briefs merely because they fall in the same BROAD area "
    "(both mention 'build', both mention 'cache') with no concrete shared subject "
    "or dependency. Link the genuinely related pairs; leave unrelated briefs "
    "unlinked — don't invent links.\n"
    "Use the EXACT keys given. Return JSON: a list \"edges\", each item "
    '{"a": "<exact key>", "b": "<exact key>", "type": '
    '"depends-on|configures|part-of|relates-to"}.'
)


def _order_briefs_by_similarity(briefs: list[dict]) -> list[dict]:
    """Reorder briefs so EMBEDDING-similar ones are adjacent (greedy
    nearest-neighbour chain), so map_scopes' fixed-size batches co-present
    topically-related briefs regardless of NAME — alphabetical batching could
    never link ``auth-service`` ↔ ``login-flow``. Falls back to the input order
    if embeddings are unavailable. Never raises."""
    try:
        from aiforge_core.memory import local_embed
        vecs = {}
        for b in briefs:
            v = local_embed.embed((b.get("summary") or b.get("key") or "")[:400])
            if any(v):
                vecs[b["key"]] = v
        if len(vecs) < 3:
            return briefs

        def _cos(a, c):
            num = sum(x * y for x, y in zip(a, c))
            da = sum(x * x for x in a) ** 0.5
            dc = sum(y * y for y in c) ** 0.5
            return num / (da * dc) if da and dc else 0.0

        remaining = [b for b in briefs if b["key"] in vecs]
        tail = [b for b in briefs if b["key"] not in vecs]
        ordered = [remaining.pop(0)]
        while remaining:
            last = vecs[ordered[-1]["key"]]
            best_i, best_s = 0, -2.0
            for i, b in enumerate(remaining):
                s = _cos(last, vecs[b["key"]])
                if s > best_s:
                    best_s, best_i = s, i
            ordered.append(remaining.pop(best_i))
        return ordered + tail
    except Exception:  # noqa: BLE001 — no embedder → keep the given order
        return briefs


def _briefs_modified_within(hours: float) -> list[str]:
    """Keys of scope briefs whose file changed within ``hours`` — the set the
    just-finished fold touched. Empty when nothing changed this cycle."""
    import time
    cutoff = time.time() - hours * 3600
    out: list[str] = []
    for p in iter_briefs():
        try:
            if p.stat().st_mtime >= cutoff:
                out.append(p.stem[len("compacted-"):])
        except OSError:
            continue
    return out


def finalize_briefs(*, role: str = "learner", recent_only: bool = False) -> dict:
    """Run the cross-brief RULES after a fold so EVERY compaction — the plain
    hourly/`Compact` path, not just `Compact all` — applies them without miss:
    merge near-duplicate topics → drop global-duplicate facts → resolve
    cross-scope contradictions (latest value wins) → sweep briefs those steps
    emptied → lint dangling links → (re)link related briefs bidirectionally.

    ``recent_only`` (the HOURLY path): the cheap deterministic rules always run,
    but the LLM steps (contradict, map_scopes) are SKIPPED when no brief changed
    in the last ``AIFORGE_COMPACT_RECENT_H`` hours (default 1) — so an idle hour
    does no model work. The nightly ``Compact all`` runs unconditionally (full).
    Each step is best-effort + idempotent; a failure never blocks the rest."""
    llm = True
    if recent_only:
        try:
            win = float(os.environ.get("AIFORGE_COMPACT_RECENT_H", "1"))
        except (TypeError, ValueError):
            win = 1.0
        # a small grace over the interval so a brief written just before the tick
        # still counts; skip the LLM rules only when truly nothing changed.
        llm = bool(_briefs_modified_within(win * 1.5))
    steps = [
        ("merge_topics", lambda: merge_similar_topics()),
        ("dedupe_global", lambda: dedupe_global_copies()),
        ("sweep_empty", lambda: sweep_empty_briefs(archive=True)),
        ("lint_graph", lambda: lint_graph(repair=True)),
    ]
    if llm:
        steps[2:2] = [("contradict", lambda: resolve_contradictions(role=role))]
        steps.append(("map_scopes", lambda: map_scopes(role=role)))
    else:
        steps.append(("llm_rules", lambda: {"skipped": "no recent brief changes"}))
    out: dict = {}
    for name, fn in steps:
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 — one rule failing must not block others
            out[name] = {"error": str(exc)}
    return out


def map_scopes(*, role: str = "learner", dry_run: bool = False) -> dict:
    """Link related scope briefs BIDIRECTIONALLY: an LLM proposes which briefs
    share subject matter (a project ↔ the global/topic brief it relates to) and
    each gets a same-dir mapping link to the other in its Links section. Gated on
    ``AIFORGE_OKR_SCOPE_LLM`` (off → no-op). Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"edges": 0, "skipped": "llm_off"}
    briefs = _live_briefs()
    if len(briefs) < 2:
        return {"edges": 0}
    by_key = {b["key"]: b for b in briefs}
    # Order by EMBEDDING similarity (not alphabetical) so a fixed-size batch
    # co-presents topically-related briefs even when their names differ — the
    # alphabetical batching left ~88% of cross-name pairs never co-presented.
    briefs = _order_briefs_by_similarity(briefs)
    lines = [f"- {b['key']}: {b['summary']}" for b in briefs]
    # BATCH so each call's listing fits the input budget — a flat listing[:cap]
    # silently hides most briefs once there are 100s of them (the edges=0 bug).
    # Small batches keep each call fast (~10s for ~35 briefs on a local 122B);
    # a big single listing times out on a cold model. AIFORGE_OKR_MAP_INPUT_CHARS
    # tunes it.
    try:
        cap = max(1500, int(os.environ.get("AIFORGE_OKR_MAP_INPUT_CHARS", "6000")))
    except (TypeError, ValueError):
        cap = 6000
    batches: list[list[str]] = []
    buf: list[str] = []
    used = 0
    for ln in lines:
        if used and used + len(ln) > cap:
            batches.append(buf)
            buf, used = [], 0
        buf.append(ln)
        used += len(ln) + 1
    if buf:
        batches.append(buf)
    raw_edges: list = []
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete
    except Exception as exc:  # noqa: BLE001
        _log.debug("map_scopes: import failed: %s", exc)
        return {"edges": 0, "error": "llm_unreachable"}

    class _Edges(BaseModel):
        edges: list[dict] = []

    # Per-batch fault isolation: one slow/failed batch (e.g. a cold-load timeout)
    # must NOT discard the edges the other batches already produced.
    failed = 0
    for i, batch in enumerate(batches, 1):
        try:
            res = structured_complete(
                role,
                [{"role": "system", "content": _MAP_SYS},
                 {"role": "user", "content": "\n".join(batch)}],
                _Edges, max_tokens=1200, max_retries=1, temperature=0.0)
            raw_edges.extend(getattr(res, "edges", None) or [])
        except Exception as exc:  # noqa: BLE001 — skip this batch, keep the rest
            failed += 1
            _log.warning("map_scopes: batch %d/%d failed: %s", i, len(batches), exc)
    if failed and not raw_edges:
        return {"edges": 0, "error": "llm_unreachable"}

    def _edge_key(e: dict, *names: str) -> str:
        for nm in names:
            v = e.get(nm)
            if v:
                return str(v).strip()
        return ""

    try:
        max_links = max(1, int(os.environ.get("AIFORGE_OKR_MAP_MAX_LINKS", "3")))
    except (TypeError, ValueError):
        max_links = 3
    # adj[key] = {other_key: relationship_type} — directed (a's type toward b),
    # with the inverse label stored on b so both ends read correctly.
    adj: dict[str, dict[str, str]] = {}
    n = 0
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        # models return {a,b} OR {from,to} OR {source,target} — accept all
        a = _edge_key(e, "a", "from", "source")
        b = _edge_key(e, "b", "to", "target")
        rel = str(_edge_key(e, "type", "rel", "relationship") or "").strip().lower()
        if rel not in _REL_INVERSE:
            rel = _REL_DEFAULT
        if a not in by_key or b not in by_key or a == b:
            continue
        if b in adj.get(a, {}):
            continue                       # already counted this undirected pair
        # Cap fan-out per brief so a loosely-linking model can't over-connect one
        # brief to a dozen others — skip the edge once EITHER end is full.
        if len(adj.get(a, ())) >= max_links or len(adj.get(b, ())) >= max_links:
            continue
        adj.setdefault(a, {})[b] = rel
        adj.setdefault(b, {})[a] = _REL_INVERSE[rel]
        n += 1
    if dry_run:
        return {"edges": n, "adj": {k: dict(v) for k, v in adj.items()}}

    # Mapping is DERIVED and fully recomputed each run: strip every brief's
    # existing sibling-brief links (keep real URLs / jira refs) and rewrite from
    # the fresh adjacency, so a re-run with a tighter prompt REMOVES stale/loose
    # links instead of piling more on. Touch ALL briefs (not just adj) so a brief
    # that lost all its links this pass is cleaned too.
    from aiforge_core.runtime import work_notes
    updated: list[str] = []
    with _WRITE_LOCK:
        for b in briefs:
            key = b["key"]
            try:
                parsed = work_notes.parse_note(
                    b["path"].read_text(encoding="utf-8"))
            except OSError:
                continue
            existing = list(parsed["sections"].get("links") or [])
            kept = [l for l in existing if not work_notes._BRIEF_REF_RE.match(l)]
            # typed sibling links: "<rel>: [key](file)" (relates-to omits the
            # prefix so a plain relation stays a plain link — clean + compatible).
            fresh = kept + [
                (f"{rel}: [{t}]({by_key[t]['file']})" if rel != _REL_DEFAULT
                 else f"[{t}]({by_key[t]['file']})")
                for t, rel in sorted(adj.get(key, {}).items())]
            if fresh == existing:
                continue                        # nothing changed for this brief
            work_notes.update_note(str(b["path"]), links=fresh,
                                   kind="knowledge", key=key)
            if adj.get(key):
                updated.append(key)
    return {"edges": n, "updated": sorted(updated)}


def _brief_file_of_source(source: str) -> str:
    """Resolve a search-hit ``source`` id back to its brief FILE name.

    Brief rows are ingested with source ``compacted:<stem>`` (Phase-3 /
    ingest_dir) or the legacy ``md:<stem>``; both map to ``<stem>.md`` where
    ``<stem>`` is ``compacted-<scope>``. Returns "" for a non-brief source."""
    s = str(source or "").strip()
    for pfx in ("compacted:", "md:"):
        if s.startswith(pfx):
            stem = s[len(pfx):]
            if stem.startswith("compacted-"):
                return stem + ".md"
    return ""


def expand_links(sources, *, max_links: int = 6, depth: int = 1) -> list[dict]:
    """Follow the **Links** section of each hit brief to its sibling briefs and
    return their FULL knowledge text.

    Search returns the briefs that matched the query; ``map_scopes`` has already
    wired each brief to its load-bearing neighbours (``[title](compacted-x.md)``
    refs in the Links section). This walks those edges so a hit surfaces the
    connected briefs' full content too — "search goes through the links and
    gives full info". Breadth-first up to ``depth`` hops, capped at
    ``max_links`` unique briefs, EXCLUDING the origin briefs themselves. Never
    raises; returns ``[{key, file, source, text, kind}]``.
    """
    from aiforge_core.runtime import work_notes
    origin = {f for f in (_brief_file_of_source(s) for s in (sources or [])) if f}
    seen: set[str] = set(origin)
    out: list[dict] = []
    frontier = list(origin)
    hop = 0
    while frontier and hop < max(1, depth) and len(out) < max_links:
        nxt: list[str] = []
        for fname in frontier:
            # briefs live in compacted/ (not the memory-dir root) — resolve there.
            p = _resolve_md(fname)
            if p is None:
                continue
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            for link in (parsed["sections"].get("links") or []):
                m = work_notes._BRIEF_REF_RE.match(link.strip())
                if not m:
                    continue                       # keep real URLs / jira refs out
                tgt = m.group("file")              # compacted-<scope>.md
                rel = (m.group("rel") or _REL_DEFAULT).strip()
                if tgt in seen:
                    continue
                seen.add(tgt)
                tp = _resolve_md(tgt)              # compacted/ subfolder
                if tp is None:
                    continue
                try:
                    d = _parse(tp)
                    text = work_notes.knowledge_text(d["body"]) or d["body"]
                except OSError:
                    continue
                key = tgt[len("compacted-"):-len(".md")]
                out.append({
                    "key": key, "file": tgt, "rel": rel,
                    "source": f"linked:{tgt[:-len('.md')]}",
                    "kind": d.get("kind") or "knowledge",
                    "title": _brief_title(key),
                    # surface the RELATIONSHIP so recall shows HOW it connects,
                    # not just that it does — the read side uses the typed link.
                    "text": f"[{rel} — via linked brief '{key}']\n{text}",
                })
                nxt.append(tgt)
                if len(out) >= max_links:
                    break
            if len(out) >= max_links:
                break
        frontier = nxt
        hop += 1
    return out[:max_links]


def _remove_facts_locked(path, key: str, remove_ci_keys: set) -> int:
    """Remove facts whose ``_ci_key(_fact_body(f))`` is in ``remove_ci_keys`` from
    a brief, re-reading it FRESH under ``_WRITE_LOCK`` so a concurrent capture
    (which also holds ``_WRITE_LOCK``) is never clobbered by a stale snapshot.
    Returns the count removed. Never raises on a bad path."""
    from aiforge_core.runtime import work_notes
    with _WRITE_LOCK:
        try:
            parsed = work_notes.parse_note(path.read_text(encoding="utf-8"))
        except OSError:
            return 0
        facts = parsed["sections"].get("facts") or []
        kept = [f for f in facts
                if work_notes._ci_key(_fact_body(f)) not in remove_ci_keys]
        removed = len(facts) - len(kept)
        if removed:
            work_notes.update_note(str(path), facts=kept, kind="knowledge",
                                   key=key)
        return removed


def reheal_scopes(*, role: str = "learner", max_per_brief: int = 60) -> dict:
    """Self-heal mis-scoped facts: re-classify each fact in every PROJECT/TOPIC
    brief and MOVE the ones that are actually global into the shared brief
    (facts captured before scope classification, or mis-hinted, end up in the
    wrong brief). The shared/global brief is never demoted into a project. Gated
    on ``AIFORGE_OKR_SCOPE_LLM`` (off → no-op). Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"moved": 0, "skipped": "llm_off"}
    from aiforge_core.runtime import work_notes
    moved = 0
    healed: list[str] = []
    # _COMPACT_LOCK (NOT _WRITE_LOCK): capture()→_brief_upsert takes _WRITE_LOCK,
    # so holding it here would self-deadlock. This serialises reheal against
    # compaction, which is the right granularity.
    with _COMPACT_LOCK:
        for b in _live_briefs():
            key = b["key"]
            if key == "shared":            # global brief — nothing to promote out
                continue
            try:
                parsed = work_notes.parse_note(b["path"].read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            if not facts:
                continue
            moved_facts: list[str] = []
            for f in facts[:max_per_brief]:
                try:
                    sc = classify_scope(f, hint_repo=key, role=role)
                except Exception:  # noqa: BLE001
                    continue
                if sc["scope"] != "global":
                    continue
                try:                              # → shared brief via capture
                    capture("learning", f, repo=None, classify=False,
                            source="reheal")
                except Exception:  # noqa: BLE001
                    continue                      # couldn't move → leave in place
                moved += 1
                moved_facts.append(f)
                # W4: drop the stale project-scoped INDEX row so the moved fact
                # isn't duplicated under both the old repo and 'shared'.
                try:
                    from aiforge_core.memory import backend_select, sqlite_memory
                    if backend_select.embedded():
                        # exclude 'compacted' so we delete the stale per-capture
                        # row, NOT the brief row (whose text contains every fact).
                        sqlite_memory.delete_by_text_contains(
                            _fact_body(f), repo=key, exclude_kind="knowledge")
                except Exception:  # noqa: BLE001
                    pass
            if not moved_facts:
                continue
            # Remove ONLY the moved facts, re-reading the brief FRESH under
            # _WRITE_LOCK — a concurrent capture that landed during the (slow)
            # classification is preserved instead of clobbered by a stale snapshot.
            _remove_facts_locked(b["path"], key, {
                work_notes._ci_key(_fact_body(x)) for x in moved_facts})
            healed.append(key)
    return {"moved": moved, "healed": healed}


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


def cleanup_reheal(*, role: str = "learner") -> dict:
    """Recovery for an over-aggressive reheal: re-classify each moved
    (``source: reheal``) fact ON ITS OWN and DELETE the ones that are not truly
    global — strip them from ``compacted-shared.md`` and remove their capture
    files. Origin repo was not recorded, so a non-global fact is removed, not
    restored to its project. Run BEFORE any recompaction reword the shared facts
    (matching is verbatim). Gated on ``AIFORGE_OKR_SCOPE_LLM``. Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"checked": 0, "removed": 0, "skipped": "llm_off"}
    from aiforge_core.runtime import work_notes
    reheal: list[dict] = []
    for p in _capture_md_files():
        if p.name.startswith("compacted-"):
            continue
        try:
            d = _parse(p)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("source") or "") != "reheal":
            continue
        head = (d.get("body") or "").strip().splitlines()
        fact = head[0].strip().lstrip("-* ").strip() if head else ""
        if fact:
            reheal.append({"path": p, "fact": fact})
    if not reheal:
        return {"checked": 0, "removed": 0}

    remove_keys: set[str] = set()
    remove_paths: list = []
    checked = 0
    for r in reheal:
        checked += 1
        try:
            sc = classify_scope(r["fact"], role=role)   # judged on its own merit
        except Exception:  # noqa: BLE001
            continue
        if sc["scope"] != "global":
            remove_keys.add(work_notes._ci_key(_fact_body(r["fact"])))
            remove_paths.append(r["path"])

    removed = 0
    matched: set[str] = set()
    if remove_keys:
        # Fresh read-modify-write under _WRITE_LOCK (concurrent captures to shared
        # also take _WRITE_LOCK — don't clobber them with a stale snapshot).
        with _WRITE_LOCK:
            shared = brief_path("shared")
            if shared.is_file():
                parsed = work_notes.parse_note(shared.read_text(encoding="utf-8"))
                facts = parsed["sections"].get("facts") or []
                kept = []
                for f in facts:
                    k = work_notes._ci_key(_fact_body(f))
                    if k in remove_keys:
                        matched.add(k)
                        removed += 1
                    else:
                        kept.append(f)
                if removed:
                    work_notes.update_note(str(shared), facts=kept,
                                           kind="knowledge", key="shared")
    # P2 shortfall guard: only delete a capture file whose fact was actually
    # found + removed from shared. A flagged fact NOT found (the shared brief was
    # reworded by a compaction since reheal) keeps its capture file, so the
    # recovery source isn't lost while an orphan remains in the brief.
    deleted = 0
    orphaned = 0
    for r in reheal:
        _rk = work_notes._ci_key(_fact_body(r["fact"]))
        if _rk not in remove_keys:
            continue                              # stays global — keep
        if _rk in matched:
            try:
                r["path"].unlink()
                deleted += 1
            except OSError:
                pass
        else:
            orphaned += 1
    if orphaned:
        _log.warning("cleanup_reheal: %d flagged fact(s) not found verbatim in "
                     "shared (reworded by a compaction?) — capture files kept "
                     "for recovery; run BEFORE a recompact next time", orphaned)
    return {"checked": checked, "removed": removed,
            "capture_files_deleted": deleted, "orphaned": orphaned}
