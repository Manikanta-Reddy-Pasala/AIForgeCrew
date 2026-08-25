"""Graph layer — mapping / lint-friendly listing / link expansion. Proposes and
writes cross-brief mapping links (bidirectional, typed) and follows them on
recall. Part of the ``_graph`` package (split from the former flat ``_graph``)."""
from __future__ import annotations

import os

from .._base import (
    _CAPTURE_SIG_RE,
    _WRITE_LOCK,
    _brief_title,
    _log,
    _parse,
    _resolve_md,
    iter_briefs,
)


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


def _cos(a, c) -> float:
    num = sum(x * y for x, y in zip(a, c))
    da = sum(x * x for x in a) ** 0.5
    dc = sum(y * y for y in c) ** 0.5
    return num / (da * dc) if da and dc else 0.0


def _brief_vectors(briefs: list[dict]) -> dict:
    from aiforge_core.memory import local_embed
    vecs = {}
    for b in briefs:
        v = local_embed.embed((b.get("summary") or b.get("key") or "")[:400])
        if any(v):
            vecs[b["key"]] = v
    return vecs


def _nearest_chain(briefs: list[dict], vecs: dict) -> list[dict]:
    """Greedy nearest-neighbour walk: start anywhere, then always take the
    closest brief still unplaced."""
    remaining = [b for b in briefs if b["key"] in vecs]
    tail = [b for b in briefs if b["key"] not in vecs]
    ordered = [remaining.pop(0)]
    while remaining:
        last = vecs[ordered[-1]["key"]]
        best_i = max(range(len(remaining)),
                     key=lambda i, last=last: _cos(last, vecs[remaining[i]["key"]]))
        ordered.append(remaining.pop(best_i))
    return ordered + tail


def _order_briefs_by_similarity(briefs: list[dict]) -> list[dict]:
    """Reorder briefs so EMBEDDING-similar ones are adjacent (greedy
    nearest-neighbour chain), so map_scopes' fixed-size batches co-present
    topically-related briefs regardless of NAME — alphabetical batching could
    never link ``auth-service`` ↔ ``login-flow``. Falls back to the input order
    if embeddings are unavailable. Never raises."""
    try:
        vecs = _brief_vectors(briefs)
        if len(vecs) < 3:
            return briefs
        return _nearest_chain(briefs, vecs)
    except Exception:  # noqa: BLE001 — no embedder → keep the given order
        return briefs


def _batched_lines(lines: list[str], cap: int) -> list[list[str]]:
    """Split the brief listing so each call fits the input budget.

    A flat listing[:cap] silently hides most briefs once there are 100s of them
    (the edges=0 bug). Small batches keep each call fast (~10s for ~35 briefs on
    a local 122B); a big single listing times out on a cold model.
    """
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
    return batches


def _propose_edges(batches: list[list[str]], role: str) -> tuple[list, int]:
    """``(raw edges, failed batch count)``.

    Per-batch fault isolation: one slow/failed batch (e.g. a cold-load timeout)
    must NOT discard the edges the other batches already produced.
    """
    from pydantic import BaseModel

    from aiforge_core.llm.structured import structured_complete

    class _Edges(BaseModel):
        edges: list[dict] = []

    raw: list = []
    failed = 0
    for i, batch in enumerate(batches, 1):
        try:
            res = structured_complete(
                role,
                [{"role": "system", "content": _MAP_SYS},
                 {"role": "user", "content": "\n".join(batch)}],
                _Edges, max_tokens=1200, max_retries=1, temperature=0.0)
            raw.extend(getattr(res, "edges", None) or [])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _log.warning("map_scopes: batch %d/%d failed: %s", i, len(batches), exc)
    return raw, failed


def _edge_key(e: dict, *names: str) -> str:
    for nm in names:
        v = e.get(nm)
        if v:
            return str(v).strip()
    return ""


def _edge_ends(e: dict) -> tuple[str, str, str]:
    """``(a, b, relationship)`` — models return {a,b} OR {from,to} OR
    {source,target}, so all three spellings are accepted."""
    rel = str(_edge_key(e, "type", "rel", "relationship") or "").strip().lower()
    return (_edge_key(e, "a", "from", "source"),
            _edge_key(e, "b", "to", "target"),
            rel if rel in _REL_INVERSE else _REL_DEFAULT)


def _adjacency(raw_edges: list, by_key: dict, max_links: int) -> tuple[dict, int]:
    """``(adj, edge count)``. ``adj[key] = {other: relationship}`` — directed
    (a's type toward b), with the inverse label stored on b so both ends read
    correctly."""
    adj: dict[str, dict[str, str]] = {}
    n = 0
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        a, b, rel = _edge_ends(e)
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
    return adj, n


def _write_links(briefs: list, by_key: dict, adj: dict) -> list[str]:
    """Mapping is DERIVED and fully recomputed each run: strip every brief's
    existing sibling-brief links (keep real URLs / jira refs) and rewrite from
    the fresh adjacency, so a re-run with a tighter prompt REMOVES stale/loose
    links instead of piling more on. Touches ALL briefs (not just adj) so a
    brief that lost all its links this pass is cleaned too."""
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
                continue                    # nothing changed for this brief
            work_notes.update_note(str(b["path"]), links=fresh,
                                   kind="knowledge", key=key)
            if adj.get(key):
                updated.append(key)
    return updated


def _int_env(key: str, default: int, low: int) -> int:
    try:
        return max(low, int(os.environ.get(key, str(default))))
    except (TypeError, ValueError):
        return default


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
    batches = _batched_lines(
        [f"- {b['key']}: {b['summary']}" for b in briefs],
        _int_env("AIFORGE_OKR_MAP_INPUT_CHARS", 6000, 1500))
    try:
        raw_edges, failed = _propose_edges(batches, role)
    except Exception as exc:  # noqa: BLE001
        _log.debug("map_scopes: import failed: %s", exc)
        return {"edges": 0, "error": "llm_unreachable"}
    if failed and not raw_edges:
        return {"edges": 0, "error": "llm_unreachable"}

    adj, n = _adjacency(raw_edges, by_key,
                        _int_env("AIFORGE_OKR_MAP_MAX_LINKS", 3, 1))
    if dry_run:
        return {"edges": n, "adj": {k: dict(v) for k, v in adj.items()}}
    return {"edges": n, "updated": sorted(_write_links(briefs, by_key, adj))}


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


def _linked_targets(fname: str):
    """``(target file, relationship)`` for each sibling-brief ref in ``fname``.
    Real URLs / jira refs are skipped."""
    from aiforge_core.runtime import work_notes
    p = _resolve_md(fname)   # briefs live in compacted/, not the memory root
    if p is None:
        return
    try:
        parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
    except OSError:
        return
    for link in (parsed["sections"].get("links") or []):
        m = work_notes._BRIEF_REF_RE.match(link.strip())
        if m:
            yield m.group("file"), (m.group("rel") or _REL_DEFAULT).strip()


def _linked_brief(tgt: str, rel: str) -> dict | None:
    """The full knowledge text of one linked brief, or None if unreadable."""
    from aiforge_core.runtime import work_notes
    tp = _resolve_md(tgt)
    if tp is None:
        return None
    try:
        d = _parse(tp)
        text = work_notes.knowledge_text(d["body"]) or d["body"]
    except OSError:
        return None
    key = tgt[len("compacted-"):-len(".md")]
    return {"key": key, "file": tgt, "rel": rel,
            "source": f"linked:{tgt[:-len('.md')]}",
            "kind": d.get("kind") or "knowledge",
            "title": _brief_title(key),
            # surface the RELATIONSHIP so recall shows HOW it connects, not
            # just that it does — the read side uses the typed link.
            "text": f"[{rel} — via linked brief '{key}']\n{text}"}


def _expand_hop(frontier: list, seen: set, out: list, max_links: int) -> list:
    """One breadth-first hop; returns the next frontier."""
    nxt: list[str] = []
    for fname in frontier:
        for tgt, rel in _linked_targets(fname):
            if tgt in seen:
                continue
            seen.add(tgt)
            brief = _linked_brief(tgt, rel)
            if brief is None:
                continue
            out.append(brief)
            nxt.append(tgt)
            if len(out) >= max_links:
                return nxt
    return nxt


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
    origin = {f for f in (_brief_file_of_source(s) for s in (sources or [])) if f}
    seen: set[str] = set(origin)
    out: list[dict] = []
    frontier = list(origin)
    for _hop in range(max(1, depth)):
        if not frontier or len(out) >= max_links:
            break
        frontier = _expand_hop(frontier, seen, out, max_links)
    return out[:max_links]
