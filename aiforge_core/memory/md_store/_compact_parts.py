"""Topic briefs split into parts: which facts go where, and folding a brief's
sections back together."""
from __future__ import annotations

from ._base import (
    _brief_part_paths,
    _log,
    _slug,
)
from ._compact_summarize import (
    _topic_split_cap,
)
from ._render import (
    _BRIEF_OBJECTIVE,
)


def _page_facts(facts: list[str], cap: int) -> list[list[str]]:
    """Split the facts into pages that each fit under ``cap`` chars."""
    pages: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for f in facts:
        if cur and size + len(f) + 3 > cap:
            pages.append(cur)
            cur, size = [], 0
        cur.append(f)
        size += len(f) + 3
    if cur:
        pages.append(cur)
    return pages or [[]]


def _part_xref(base: str, i: int, n: int) -> list[str]:
    """The cross-reference back to part 1 / forward to the next — the "split and
    refer" pattern. Empty for a single-page topic."""
    if n <= 1:
        return []
    xref = []
    if i > 0:
        xref.append(f"**Part {i + 1} of {n}** · main topic: "
                    f"[{base}](compacted-{base}.md)")
    if i < n - 1:
        xref.append(f"**Continued in:** "
                    f"[part {i + 2}](compacted-{base}-{i + 2}.md)")
    return xref


def _brief_parts(key: str, sections: dict, tags, title: str,
                 sources: list[str] | None = None,
                 body_md: str = "") -> list[tuple[str, str]]:
    """Render an OKR knowledge brief → ``[(stem, content), …]``. Facts are paged
    under the split cap: a topic that fits is ONE file; a topic that outgrows it
    splits into compacted-<key>.md + compacted-<key>-2.md … each carrying the
    OKR envelope (kind/tags/objective) and a cross-reference back to part 1 /
    forward to the next (the "split and refer" pattern). Key Results + Learnings
    stay on part 1 (the canonical head) — and so does ``sources``, the
    provenance of the whole fold: one claim per topic, on its canonical head.
    ``body_md`` (the no-model path's consolidated prose) also rides part 1."""
    from aiforge_core.runtime import work_notes
    pages = _page_facts([str(f) for f in (sections.get("facts") or [])],
                        _topic_split_cap())
    n = len(pages)
    base = _slug(key)
    parts: list[tuple[str, str]] = []
    for i, page in enumerate(pages):
        stem = f"compacted-{base}" if i == 0 else f"compacted-{base}-{i + 1}"
        parts.append((stem, _render_part(work_notes, key, base, sections, tags,
                                         title, sources, page, i, n, body_md)))
    return parts


def _render_part(work_notes, key: str, base: str, sections: dict, tags,
                 title: str, sources, page: list, i: int, n: int,
                 body_md: str = "") -> str:
    """One page of a (possibly split) brief. The canonical head (part 1) is the
    only part that carries Key Results, Learnings and the fold's provenance."""
    first = i == 0
    body = "\n\n".join(x for x in ([body_md] if first else [])
                       + _part_xref(base, i, n) if x)
    return work_notes.render_note(
        "knowledge", key if first else f"{key}-{i + 1}",
        title=(title if n == 1 else f"{title} (part {i + 1}/{n})"),
        objective=_BRIEF_OBJECTIVE.format(key=key),
        key_results=((sections.get("key_results") or []) if first else None),
        facts=page, links=(sections.get("links") or []),
        learnings=((sections.get("learnings") or []) if first else None),
        sources=(sources if first else None),
        tags=tags, body_md=body)


def _union_back(new_list, old_list) -> list:
    """Recover items from ``old_list`` missing from ``new_list`` so an LLM fold
    can't silently DROP curated content (Learnings / Key Results / Links). The
    recovered (older) items are PREPENDED, keeping ``new_list`` (the LLM's
    current view / the chronological tail) LAST — so a downstream ``[-N:]``
    recency cap still selects the newest, not the resurrected old ones."""
    new = list(new_list or [])
    missing = [x for x in (old_list or []) if x not in new]
    return missing + new


def _existing_brief_sections(path, base: str) -> tuple[dict, list]:
    """``(sections, prior tags)`` read from the primary brief AND every
    split-out part (compacted-<key>-N.md) — so a re-fold NEVER loses facts that
    a previous oversize split moved into part 2+."""
    from aiforge_core.runtime import work_notes
    existing: dict = {"facts": [], "learnings": [], "links": [],
                      "key_results": [], "objective": ""}
    prev_tags: list = []
    for pp in [path] + _brief_part_paths(base):
        if not pp.exists():
            continue
        parsed = work_notes.parse_note(
            pp.read_text(encoding="utf-8", errors="replace"))
        _absorb_part(existing, parsed["sections"])
        prev_tags += list((parsed["frontmatter"] or {}).get("tags") or [])
    return existing, prev_tags


def _absorb_part(existing: dict, sec: dict) -> None:
    """Union one part's sections into the accumulating brief."""
    existing["objective"] = existing["objective"] or (sec.get("objective") or "")
    for fld in ("facts", "learnings", "links", "key_results"):
        for it in sec.get(fld) or []:
            if it not in existing[fld]:
                existing[fld].append(it)


def _refold_content(blocks: list[str], existing: dict) -> str:
    """The text handed to the consolidator.

    RE-FOLD a fact-only brief: force compaction adds every existing brief as an
    empty-live group, and a brief that carries Facts but no consolidated PROSE
    body yields blocks=[] → "" → consolidate() takes its no-LLM "nothing new"
    path, so the force pass did zero real work (270 briefs in 8s, no model
    calls). Feeding the brief's existing Facts back makes the LLM genuinely
    re-consolidate them. Only fires when there is no new content — normal
    compaction always has live items, so this is a no-op there.
    """
    new_content = "\n\n".join(b for b in blocks if b.strip())
    if new_content.strip() or not existing.get("facts"):
        return new_content
    return "\n".join(f"- {f}" for f in existing["facts"])


def _consolidate_brief_sections(key: str, path, blocks: list[str],
                                model_role: str, tags) -> tuple[dict, list]:
    """LLM-consolidate the group into OKR sections (dedupe/map/supersede via
    work_notes.consolidate) and return ``(sections, merged_tags)``. Prior
    hand-added Learnings + the brief's prior tags are preserved."""
    from aiforge_core.runtime import work_notes
    existing, prev_tags = _existing_brief_sections(path, _slug(key))
    merged = work_notes.consolidate(
        existing, _refold_content(blocks, existing), role=model_role,
        label=f"topic '{key}' ({len(blocks)} source(s))")
    # Deterministic UNION-BACK of derived/curated sections the LLM might omit:
    # Learnings (audit trail), Key Results (write-time W2 tickets) and Links
    # (map_scopes sibling links). Without this a single fold that drops them
    # loses that content permanently on the daily recompact.
    for fld in ("learnings", "key_results", "links"):
        merged[fld] = _union_back(merged.get(fld), existing.get(fld))
    merged["facts"] = _kept_facts(merged.get("facts"), existing.get("facts"), key)
    return merged, list(prev_tags) + list(tags or [])


# A fold may legitimately shrink the Facts list (dedupe, supersede), but not to
# nothing and not to a sliver — below this share of what went in, the result is
# treated as a failed fold rather than as the brief's new truth.
_FOLD_FLOOR = 0.25


def _kept_facts(new_facts, old_facts, key: str) -> list:
    """The folded Facts — unless the fold collapsed them, in which case the
    brief keeps what it had.

    The LLM's Facts list REPLACES the brief's (that is what consolidation is
    for), so one truncated, refused or malformed reply could erase a brief's
    entire knowledge with nothing to rebuild it from. Learnings, Key Results and
    Links are already union-backed; this is the same protection for the section
    that carries the most."""
    new = list(new_facts or [])
    old = list(old_facts or [])
    if not old or len(new) >= max(1, int(len(old) * _FOLD_FLOOR)):
        return new
    _log.warning("compact: fold of '%s' returned %d of %d facts — keeping the "
                 "brief's own facts (treated as a failed fold)",
                 key, len(new), len(old))
    return _union_back(new, old)


def _consolidate_brief_content(key: str, path, blocks: list[str], title: str,
                               model_role: str,
                               tags: list[str] | None = None) -> str:
    """Build an OKR knowledge brief by LLM-consolidating this group's notes.

    Folds ``blocks`` (the group's units + any prior consolidated body) into the
    prior brief's OKR sections via ``work_notes.consolidate`` — dedupe
    paraphrases, resolve contradictions (newer supersedes), MAP each item to
    Objective/Key Results/Facts/Links/Learnings; chonkie chunks large input.
    Prior hand-added Learnings (the audit trail) are unioned back in so the LLM
    can never drop them. consolidate() degrades to a deterministic union+dedupe
    when no model is reachable, so this never loses content."""
    from aiforge_core.runtime import work_notes
    existing: dict = {}
    prev_tags: list = []
    if path.exists():
        _parsed = work_notes.parse_note(
            path.read_text(encoding="utf-8", errors="replace"))
        existing = _parsed["sections"]
        prev_tags = list((_parsed["frontmatter"] or {}).get("tags") or [])
    new_content = "\n\n".join(b for b in blocks if b.strip())
    merged = work_notes.consolidate(existing, new_content, role=model_role)
    # Union-back the derived/curated sections the LLM might drop (see
    # _consolidate_brief_sections): Learnings, Key Results, Links.
    learnings = _union_back(merged.get("learnings"), existing.get("learnings"))
    key_results = _union_back(merged.get("key_results"),
                              existing.get("key_results"))
    links = _union_back(merged.get("links"), existing.get("links"))
    # union the group's tags with the brief's prior tags (render normalizes/dedupes)
    all_tags = list(prev_tags) + list(tags or [])
    return work_notes.render_note(
        "knowledge", key, title=title,
        objective=_BRIEF_OBJECTIVE.format(key=key),
        key_results=key_results, facts=merged.get("facts"),
        links=links, learnings=learnings, body_md="", tags=all_tags)


# How many consumed capture stems a brief carries. Provenance is a hand-off
# note, not an archive: only a peer that has NOT yet archived a capture cares,
# and it sees the claim within a cycle or two. The cap keeps a long-lived brief
# from growing an unbounded frontmatter; a stem that ages out simply leaves that
# capture un-archived on a peer that never saw the claim — untidy, never lost.
_SOURCES_CAP = 400


def _fold_sources(prior: list[str], consumed: list[str]) -> list[str]:
    """Prior claims + the stems this fold just consumed, newest last, capped."""
    return list(dict.fromkeys(list(prior) + list(consumed)))[-_SOURCES_CAP:]
