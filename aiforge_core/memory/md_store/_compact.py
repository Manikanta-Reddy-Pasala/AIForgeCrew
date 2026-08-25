"""md_store internals: the compaction pipeline — per-run capture sweeps, LLM
summarise/consolidate helpers, grouping + split-part rendering and the
`compact()` / `cleanup_legacy_compacted()` drivers. Builds on `_base`,
`_render`, `_ingest` and `_capture`."""
from __future__ import annotations

import os
import re

from ._base import (
    _FM_RE,
    _COMPACT_LOCK,
    _WRITE_LOCK,
    _brief_part_paths,
    _brief_title,
    _capture_md_files,
    _log,
    _md_path_for_stem,
    _now_iso,
    _parse,
    _resolve_md,
    _slug,
    iter_briefs,
    memory_dir,
)
from ._capture import capture
from ._ingest import _ingest_unit
from ._render import (
    _BRIEF_OBJECTIVE,
    _parse_brief,
    _render_brief,
    brief_source_stems,
)

_N_N_N_N = '\\n\\n---\\n\\n'

# Sentinel topic key for a note the topic labeller couldn't theme. Such a note
# already lives in its repo/shared brief, so it must NOT spawn a topic file —
# and it must NEVER fall back to the note's KIND (that minted the junk
# compacted-learning.md / compacted-user-comment.md briefs). `compact()` drops
# this group before writing.
_NO_TOPIC = "\x00no-topic"


# Per-run capture signature: <slug>-YYYYMMDD-<6hex>.md
_CAPTURE_SIG_SUFFIX_RE = re.compile(r"-\d{8}-[0-9a-f]{6}\.md$")


def _retire_brief(path, dst, archive: bool) -> bool:
    """Move the file into ``dst`` (reversible), or delete it. False on failure."""
    import shutil
    try:
        if archive:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dst / path.name))
        else:
            path.unlink()
        return True
    except OSError:
        return False


def sweep_stale_captures(*, archive: bool = True) -> dict:
    """Retire per-run capture files that MASQUERADE as canonical briefs.

    A capture is stamped ``<slug>-YYYYMMDD-<6hex>.md``. When its title happened
    to start with "compacted" (e.g. the legacy-cleanup re-writing a brief's own
    title) the slug became ``compacted-…`` — and ``compact()`` excludes every
    ``compacted-*`` file from its live set, so these transient captures slip
    past compaction FOREVER and accumulate (``compacted-retry-on-empty-fix`` &
    friends). Their facts are already folded into the real
    ``compacted-<topic>.md`` brief by ``_brief_upsert`` at write time, so they
    carry nothing new.

    Moves each masquerader into ``archive/<ts>/`` (reversible; ``archive=False``
    deletes). Canonical briefs — ``compacted-<topic>.md`` with NO date-hex
    suffix — are untouched. Runs in the hourly compaction. Never raises."""
    swept: list[str] = []
    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    try:
        with _COMPACT_LOCK:
            for p in iter_briefs():
                if not _CAPTURE_SIG_SUFFIX_RE.search(p.name):
                    continue                    # real canonical brief — keep
                if _retire_brief(p, dst, archive):
                    swept.append(p.name)
    except Exception as exc:  # noqa: BLE001 — sweep is best-effort upkeep
        return {"ok": False, "error": str(exc), "swept": len(swept)}
    return {"ok": True, "swept": len(swept), "archived": archive,
            "files": swept}


def _is_dead_brief(path) -> bool:
    """A brief carrying ONLY the boilerplate Objective.

    Links matter here: map_scopes links are BIDIRECTIONAL, so deleting a
    links-only brief orphans its sibling's inbound link.
    """
    from aiforge_core.runtime import work_notes
    try:
        parsed = work_notes.parse_note(
            path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return False
    sec = parsed.get("sections") or {}
    return not (sec.get("facts") or sec.get("learnings")
                or sec.get("key_results") or sec.get("links")
                or (parsed.get("body") or "").strip())


def sweep_empty_briefs(*, archive: bool = True) -> dict:
    """Retire DEAD canonical briefs — a ``compacted-<key>.md`` that carries only
    the boilerplate Objective with NO Facts, Key results, Learnings, or body.

    These accumulate when a topic's facts all migrate into another brief (the
    labeller re-clusters), when a fact-only brief is emptied, or from legacy
    ``compacted-compacted-*`` double-fold artifacts — leaving a stub that shows
    up as an "empty" memory but holds no knowledge. Moves each into
    ``archive/<ts>/`` (reversible; ``archive=False`` deletes). A brief with ANY
    real content is never touched. Never raises."""
    swept: list[str] = []
    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    try:
        with _COMPACT_LOCK:
            for p in iter_briefs():
                if _CAPTURE_SIG_SUFFIX_RE.search(p.name):
                    continue          # a capture — sweep_stale_captures owns it
                if _is_dead_brief(p) and _retire_brief(p, dst, archive):
                    swept.append(p.name)
    except Exception as exc:  # noqa: BLE001 — best-effort upkeep
        return {"ok": False, "error": str(exc), "swept": len(swept)}
    return {"ok": True, "swept": len(swept), "archived": archive, "files": swept}


def _demote_headings(body: str, by: int = 2) -> str:
    """Push every markdown heading in ``body`` ``by`` levels deeper (capped at
    h6) so an embedded ``# Title`` doesn't collide with the ``##`` section
    wrapper a compacted file gives each source note."""
    out = []
    for line in body.splitlines():
        m = re.match(r"^(#{1,6})(\s)", line)
        if m:
            lvl = min(6, len(m.group(1)) + by)
            out.append("#" * lvl + line[len(m.group(1)):])
        else:
            out.append(line)
    return "\n".join(out)


_SUMMARY_SYS = (
    "You consolidate engineering memory notes. Merge the notes below into ONE "
    "concise markdown document. Deduplicate ruthlessly, group related points "
    "under '## ' section headings, and KEEP every concrete fact, decision, "
    "gotcha, file path, command, id and number. Drop chit-chat, repetition and "
    "filler. Do not invent anything. Output ONLY the markdown body — no preamble, "
    "no surrounding code fence."
)
# Upper bound on the notes sent to ONE summarize call. A ceiling, not the
# budget: the real limit is whatever the role's context window leaves after the
# completion (``_summary_input_cap``). A bare constant is a guess about a
# window rather than a reading of it — too small for a 262k model (needless
# map-reduce passes over text that would fit in one) and too large the moment
# a role points at a 32k one, which is how a fold ends up 400ing on length.
_SUMMARY_INPUT_CAP = 28_000
_COMPACT_BODY_CAP = 60_000      # max chars of a deterministic-merge consolidated
                                # file (bounds growth when no model is reachable)


_SUMMARY_OUT_TOKENS = 4096      # a brief, not a book


def _summary_input_cap(role: str) -> int:
    """Chars of notes one summarize call may carry, for THIS role's window.

    Shares the rule with the OKR fold (``work_notes._consolidate``) rather than
    keeping a second constant that drifts against it.
    """
    try:
        from aiforge_core.runtime.work_notes._consolidate import input_char_budget
        return max(4000, min(_SUMMARY_INPUT_CAP,
                             input_char_budget(role, output_tokens=_SUMMARY_OUT_TOKENS)))
    except Exception:  # noqa: BLE001 — budgeting must never break compaction
        return _SUMMARY_INPUT_CAP


def _summarize_block(text: str, role: str) -> str | None:
    """One LLM consolidation call. Returns markdown, or None on any failure
    (model down / unknown role / empty) so the caller falls back to merge."""
    cap = _summary_input_cap(role)
    if len(text) > cap:
        # Refused rather than sent: an oversized block is what produces a
        # context-length 400, and a 400 fails the WHOLE compaction (this
        # function returning None bails the entire op to a deterministic
        # merge). The caller splits and retries instead.
        return None
    try:
        from aiforge_core.llm.client import complete
        out = complete(
            role,
            [{"role": "system", "content": _SUMMARY_SYS},
             {"role": "user", "content": text}],
            temperature=0.2, max_tokens=_SUMMARY_OUT_TOKENS,
        )
    except Exception:  # noqa: BLE001 — any failure → deterministic merge
        return None
    out = (out or "").strip()
    # strip an accidental wrapping ```/```md fence
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out).strip()
    return out or None


def _split_to_cap(block: str, cap: int) -> list[str]:
    """Split ONE oversized block on line boundaries so every piece fits.

    A single block can exceed the cap on its own — one enormous capture, a
    pasted log. Batching alone never split it, so it went to the model whole,
    400'd on length, and bailed the whole compaction.
    """
    if len(block) <= cap:
        return [block]
    out, buf, used = [], [], 0
    for line in (block or "").splitlines(keepends=True):
        if used and used + len(line) > cap:
            out.append("".join(buf))
            buf, used = [], 0
        buf.append(line)
        used += len(line)
    if buf:
        out.append("".join(buf))
    return out


def _batch_under_cap(blocks: list[str], cap: int) -> list[str]:
    """Greedily batch blocks under the input cap."""
    batches: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for b in blocks:
        if cur and cur_len + len(b) > cap:
            batches.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += len(b)
    if cur:
        batches.append("\n\n".join(cur))
    return batches


def _summarize_notes(blocks: list[str], role: str) -> str | None:
    """Map-reduce consolidation: summarize the notes (batched to fit the input
    cap), then summarize the partial summaries if there was more than one
    batch. Returns markdown or None (→ caller merges deterministically)."""
    if not blocks:
        return None
    cap = _summary_input_cap(role)
    sized = [piece for b in blocks for piece in _split_to_cap(b, cap)]
    partials: list[str] = []
    for batch in _batch_under_cap(sized, cap):
        s = _summarize_block(batch, role)
        if s is None:
            return None          # bail whole op → deterministic merge
        partials.append(s)
    if len(partials) == 1:
        return partials[0]
    # reduce step — combine the partial summaries
    combined = "\n\n---\n\n".join(partials)
    if len(combined) <= cap:
        return _summarize_block(combined, role) or combined
    return combined          # already summarized; accept as-is if still huge


def _snap_known_topics(files, known):
    """Deterministically snap each note to the nearest known topic over the cutoff (no LLM). Returns (labels, leftover_notes, shortlist_of_nearest)."""
    from . import _topics
    labels: dict = {}
    leftover: list[dict] = []
    shortlist: list[str] = []
    vec_cache: dict = {}

    for d in files:
        text = (d.get("title") or "") + "\n" + (d.get("body") or "")[:400]
        hit, near = _topics.snap_by_similarity(text, known, _cache=vec_cache)
        if not shortlist and near:
            shortlist = near
        if hit:
            labels[d["file"]] = hit
        else:
            leftover.append(d)
    return labels, leftover, shortlist


def _topic_labels(files: list[dict], role: str) -> dict:
    """Map ``{file_name: topic-slug}`` for a compaction pass.

    Deterministic FIRST: each note is scored against the topic briefs already
    on disk and snapped to the nearest one over the cutoff — no LLM, no drift,
    and the vocabulary stays stable across runs. Only the notes with no home
    reach the model, and it sees just those titles plus a shortlist of the
    nearest existing topics (never all ~140), because handing the model the
    whole vocabulary is itself a drift source.

    Every slug — snapped or invented — then passes admission control
    (:func:`_topics.admit`), so generic magnets (``code``/``data``/``tmp``) and
    junk (``m``/``na2``) never mint a file. Returns ``{}`` on total failure;
    the caller falls back to kind grouping.
    """
    if not files:
        return {}
    from . import _topics
    from ._scope import _snap_topic

    known = _topics.existing_topics()
    labels, leftover, shortlist = _snap_known_topics(files, known)

    if leftover:
        for f, slug in _llm_topic_labels(leftover, role, shortlist).items():
            labels[f] = slug

    out: dict = {}
    for f, slug in labels.items():
        ok = _topics.admit(slug, _snap_topic)
        if ok:
            out[f] = ok
    return out


def _llm_topic_labels(files: list[dict], role: str,
                      shortlist: list[str]) -> dict:
    """Ask the model to theme ONLY the notes the deterministic snap could not
    place, choosing from ``shortlist`` when one fits. Small payload by design:
    the leftover titles and at most a dozen candidate topics. Empty on any
    failure — those notes then stay in their repo/shared brief."""
    if len(files) < 1:
        return {}
    listing = "\n".join(f"{i}: {(d.get('title') or d.get('file') or '')[:80]}"
                        for i, d in enumerate(files))
    known = ("\nEXISTING TOPICS (prefer one of these): "
             + ", ".join(shortlist)) if shortlist else ""
    try:
        from pydantic import RootModel

        from aiforge_core.llm.structured import structured_complete

        class _Topics(RootModel[dict]):
            pass

        raw = structured_complete(role, [
            {"role": "system", "content":
             "Assign each memory-note title a SUBJECT topic. Reuse an existing "
             "topic whenever it fits; only invent a slug when none does. A new "
             "slug must name a real subject (2-4 words, kebab-case) — never a "
             "generic word like code/data/file/test/build and never an "
             "abbreviation. Reply ONLY a JSON object mapping each index (as a "
             'string) to its topic slug, e.g. {"0":"data-sync"}. '
             "Every index must appear once." + known},
            {"role": "user", "content": listing[:4000]},
        ], _Topics, max_tokens=600, max_retries=1, temperature=0.0).root
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    import re as _re
    labels: dict = {}
    for k, v in raw.items():
        try:
            idx = int(k)
        except (ValueError, TypeError):
            continue
        if 0 <= idx < len(files) and isinstance(v, str) and v.strip():
            slug = _re.sub(r"[^a-z0-9]+", "-", v.strip().lower()).strip("-")[:40]
            if slug:
                labels[files[idx]["file"]] = slug
    return labels


def _repo_key(d: dict) -> str:
    """Project-brief axis: one consolidated file per repo. An explicit
    frontmatter ``repo``, else a ``repo:<x>`` tag, else "shared" (cross-repo)."""
    if d.get("repo"):
        return d["repo"]
    for t in d.get("tags") or []:
        if t.startswith("repo:"):
            return t.split(":", 1)[1] or "shared"
    return "shared"


def _topic_key(d: dict) -> str:
    """Topic axis: an explicit frontmatter ``topic`` or a ``topic:<slug>`` tag
    wins (no LLM needed); else the precomputed label; else UNGROUPED.

    A note the labeller couldn't theme returns _NO_TOPIC (dropped by compact())
    — it must NOT fall back to the note's KIND, which minted junk briefs like
    compacted-learning.md / compacted-user-comment.md.
    """
    if d.get("topic"):
        return d["topic"]
    for t in d.get("tags") or []:
        if t.startswith("topic:"):
            return t.split(":", 1)[1] or _NO_TOPIC
    return d.get("_topic") or _NO_TOPIC


def _group_key(d: dict, group_by: str) -> str:
    if group_by == "repo":
        return _repo_key(d)
    if group_by == "topic":
        return _topic_key(d)
    if group_by == "tag":
        return (d["tags"][0] if d.get("tags") else "untagged")
    if group_by == "source":
        # the leading token of the source key (e.g. "chat", "md", "ticket")
        return (d.get("source") or "manual").split(":", 1)[0].split("-", 1)[0]
    return d.get("kind") or "note"


def _topic_split_cap() -> int:
    """Facts-size (chars) beyond which a topic brief SPLITS into linked parts.
    A major topic that outgrows this becomes compacted-<topic>.md +
    compacted-<topic>-2.md … cross-referenced. Env AIFORGE_TOPIC_SPLIT_CAP."""
    try:
        return max(500, int(os.environ.get("AIFORGE_TOPIC_SPLIT_CAP", "12000")))
    except (TypeError, ValueError):
        return 12000


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
                 sources: list[str] | None = None) -> list[tuple[str, str]]:
    """Render an OKR knowledge brief → ``[(stem, content), …]``. Facts are paged
    under the split cap: a topic that fits is ONE file; a topic that outgrows it
    splits into compacted-<key>.md + compacted-<key>-2.md … each carrying the
    OKR envelope (kind/tags/objective) and a cross-reference back to part 1 /
    forward to the next (the "split and refer" pattern). Key Results + Learnings
    stay on part 1 (the canonical head) — and so does ``sources``, the
    provenance of the whole fold: one claim per topic, on its canonical head."""
    from aiforge_core.runtime import work_notes
    pages = _page_facts([str(f) for f in (sections.get("facts") or [])],
                        _topic_split_cap())
    n = len(pages)
    base = _slug(key)
    parts: list[tuple[str, str]] = []
    for i, page in enumerate(pages):
        stem = f"compacted-{base}" if i == 0 else f"compacted-{base}-{i + 1}"
        parts.append((stem, _render_part(work_notes, key, base, sections, tags,
                                         title, sources, page, i, n)))
    return parts


def _render_part(work_notes, key: str, base: str, sections: dict, tags,
                 title: str, sources, page: list, i: int, n: int) -> str:
    """One page of a (possibly split) brief. The canonical head (part 1) is the
    only part that carries Key Results, Learnings and the fold's provenance."""
    first = i == 0
    return work_notes.render_note(
        "knowledge", key if first else f"{key}-{i + 1}",
        title=(title if n == 1 else f"{title} (part {i + 1}/{n})"),
        objective=_BRIEF_OBJECTIVE.format(key=key),
        key_results=((sections.get("key_results") or []) if first else None),
        facts=page, links=(sections.get("links") or []),
        learnings=((sections.get("learnings") or []) if first else None),
        sources=(sources if first else None),
        tags=tags, body_md="\n\n".join(_part_xref(base, i, n)))


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
    return merged, list(prev_tags) + list(tags or [])


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


def archive_covered_captures() -> dict:
    """Archive local captures that an ARRIVED brief already claims to have eaten.

    Ordinarily a machine archives the captures its OWN fold just consumed. This
    is the other half: a brief that arrived from elsewhere, claiming captures we
    also hold, lets us tidy them up without re-distilling them.

    Soft-fails CLOSED, the opposite of the distillation gate, and deliberately:
    archiving a capture no brief covers destroys an un-distilled memory and
    nothing can rebuild it, while failing to archive one that IS covered leaves
    a tidy-up for the next cycle. Any doubt at all → move nothing.
    """
    import shutil
    try:
        covered = brief_source_stems()
    except Exception as exc:  # noqa: BLE001 — see docstring: uncertainty ⇒ nothing
        _log.info("compact: provenance unreadable (%s) — archiving nothing", exc)
        return {"archived": 0, "housekeeping": "provenance-unreadable"}
    if not covered:
        # No brief claims anything (e.g. every brief predates provenance) —
        # nothing is PROVABLY distilled, so nothing may be moved.
        return {"archived": 0}

    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    moved: list[str] = []
    with _COMPACT_LOCK, _WRITE_LOCK:
        for p in _capture_md_files():
            if p.stem not in covered:
                continue
            try:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dst / p.name))
                moved.append(p.name)
            except OSError:      # keep the capture; the next cycle retries
                continue
    if moved:
        _log.info("compact: archived %d capture(s) already covered by a brief",
                  len(moved))
    return {"archived": len(moved), "archived_files": moved,
            "archive": str(dst)}


def _live_capture_notes(group_by: str) -> list[dict]:
    """The raw capture units eligible for this axis.

    The REPO brief is a curated project-learning projection — raw per-session
    transcripts (kind="session") stay out of it. The TOPIC axis is exactly
    where sessions belong: memory organized BY TOPIC. Large transcripts are
    fine there now — consolidate() distils them via the LLM (chonkie chunks big
    input) into Facts/Learnings rather than dumping, and the raw file archives
    out after folding. Excluding them was why per-session memory lingered and
    compaction said "nothing to compact".
    """
    files: list[dict] = []
    for p in _capture_md_files():
        try:
            d = _parse(p)
        except Exception:  # noqa: BLE001
            continue
        d["_path"] = p
        files.append(d)
    live = [d for d in files if not d["file"].startswith("compacted-")]
    if group_by == "repo":
        live = [d for d in live if (d.get("kind") or "") != "session"]
    return live


def _label_topics(live: list[dict], model_role: str) -> None:
    """TOPIC mode: one LLM pass labels every note with a coherent topic slug so
    compaction yields several browsable topical files instead of ONE blob per
    kind. Falls back to kind grouping for any note the labeller missed (or all,
    if the model is unreachable)."""
    try:
        labels = _topic_labels(live, model_role)
    except Exception:  # noqa: BLE001
        labels = {}
    for d in live:
        d["_topic"] = labels.get(d["file"])


def _explicit_topic(d: dict) -> str | None:
    return d.get("topic") or next(
        (t.split(":", 1)[1] for t in (d.get("tags") or [])
         if t.startswith("topic:")), None)


def _apply_topic_floor(groups: dict, live: list[dict]) -> dict:
    """A brand-new MODEL-INVENTED topic must earn its file: below the floor its
    notes stay in their repo/shared brief instead of minting a one-fact magnet
    (the `compacted-isprime-function.md` class of junk). Topics that already
    exist keep receiving regardless, and a topic the CALLER named explicitly is
    intentional — never gated."""
    from . import _topics
    floor = _topics.min_facts_for_new_topic()
    if floor <= 1:
        return groups
    keep = set(_topics.existing_topics())
    keep.update(t for t in (_explicit_topic(d) for d in live) if t)
    return {k: v for k, v in groups.items() if k in keep or len(v) >= floor}


def _add_existing_briefs(result: dict) -> None:
    """force: re-consolidate every EXISTING brief too — add each
    compacted-<scope>.md as its own group so the loop re-reads + re-summarises
    it even with no new live sources. Split-part / per-run-named files are
    skipped (they fold via their primary scope)."""
    for p in iter_briefs():
        if re.search(r"-\d{8}-[0-9a-f]{6}$", p.stem):
            continue
        key = p.stem[len("compacted-"):] or "shared"
        result.setdefault(key, [])   # empty live → existing_body re-consolidated


def _gather_planned(group_by: str, min_group: int, model_role: str,
                    force: bool) -> dict[str, list[dict]]:
    """``{group key: notes}`` for the groups this run will fold."""
    live = _live_capture_notes(group_by)
    if group_by == "topic":
        _label_topics(live, model_role)
    groups: dict[str, list[dict]] = {}
    for d in live:
        groups.setdefault(_group_key(d, group_by), []).append(d)
    if group_by == "topic":
        # Notes the labeller couldn't theme (_NO_TOPIC) must not form a topic
        # file — they already live in their repo/shared brief.
        groups.pop(_NO_TOPIC, None)
        groups = _apply_topic_floor(groups, live)
    result = {k: v for k, v in groups.items() if len(v) >= min_group}
    if force:
        _add_existing_briefs(result)
    return result


def _prior_brief_state(path, group_by: str) -> tuple[str, list]:
    """``(existing consolidated body, prior sources)`` for a re-compaction.

    The previous file is fed back so it gets RE-SUMMARISED with the new notes,
    keeping the file bounded. For the knowledge axes (repo/topic) it is an OKR
    envelope, so only the prior consolidated PROSE is re-fed — otherwise the
    envelope text would nest inside the new body every compaction.
    """
    if not path.exists():
        return "", []
    prev = path.read_text(encoding="utf-8", errors="replace")
    prior_sources = _parse_brief(prev)["sources"]
    if group_by in ("repo", "topic"):
        return _parse_brief(prev)["body"].strip(), prior_sources
    pm = _FM_RE.match(prev)
    return (pm.group(2).strip() if pm else prev.strip()), prior_sources


def _group_blocks(items: list[dict], existing_body: str,
                  title: str) -> tuple[list[str], list[str]]:
    """``(deterministic sections, LLM blocks)`` for one group."""
    sections, blocks = [], []
    if existing_body:
        blocks.append("### (previous consolidated)\n\n" + existing_body)
    for d in items:
        meta = (f"_source: {d.get('source') or 'manual'} · "
                f"created: {d.get('created') or '?'}_")
        sections.append(f"## {d['title']}\n\n{meta}\n\n"
                        f"{_demote_headings(d['body']).strip()}".rstrip())
        blocks.append(f"### {d['title']}\n\n{d['body'].strip()}")
    return sections, blocks


def _capped_merge(body: str, title: str) -> str:
    """Bound the deterministic-merge fallback so an always-down model can't grow
    the file every run (the "file too big" problem)."""
    if len(body) <= _COMPACT_BODY_CAP:
        return body
    head = f"# {title}\n\n"
    keep = max(1000, _COMPACT_BODY_CAP - len(head) - 80)
    return (head + "_…older entries trimmed (kept in archive/); configure a "
            "model so compaction can summarise._\n\n---\n\n" + body[-keep:])


def _kind_frontmatter(title: str, stem: str, all_tags: list, count: int,
                      did_summarize: bool, fold_sources: list) -> str:
    return ("---\n"
            f"title: {title}\n"
            "kind: compacted\n"
            f"tags: {', '.join(all_tags)}\n"
            f"source: compacted:{stem}\n"
            f"created: {_now_iso()}\n"
            f"count: {count}\n"
            f"summarized: {str(did_summarize).lower()}\n"
            + "".join(["sources:\n"] + [f"  - {s}\n" for s in fold_sources]
                      if fold_sources else [])
            + "---\n\n")


def _prepare_group(key: str, items: list[dict], *, group_by: str,
                   summarize: bool, model_role: str,
                   archive_sources: bool) -> dict:
    """Fold ONE group into its file part(s) (the slow, LLM half — no
    _WRITE_LOCK held, so concurrent chat-turn writes aren't frozen)."""
    items.sort(key=lambda d: d.get("created") or "")
    all_tags = sorted({t for d in items for t in d.get("tags") or []})
    title = f"{key.replace('-', ' ').strip().capitalize()} memory (compacted)"
    stem = f"compacted-{_slug(key)}"
    path = _md_path_for_stem(stem)
    existing_body, prior_sources = _prior_brief_state(path, group_by)
    # PROVENANCE: the stems this brief now carries, so a peer that received it
    # can archive its own copies of them. Claimed only when we are actually
    # archiving the originals — in projection mode the units stay alive for the
    # OTHER axis, and a peer must not tidy away what this axis has not consumed.
    fold_sources = _fold_sources(
        prior_sources,
        [d["_path"].stem for d in items] if archive_sources else [])
    sections, blocks = _group_blocks(items, existing_body, title)

    # Knowledge axes (repo/topic) with a model → STRUCTURED consolidation into
    # real OKR sections (Facts/Links/Learnings) via work_notes.consolidate
    # (dedupe / map / supersede; chonkie chunks big input). The prose-summary +
    # deterministic-merge paths stay for the kind axis and the no-model case.
    use_structured = (group_by in ("repo", "topic")) and summarize
    if use_structured:
        # Facts are paged: a topic that outgrows the split cap becomes several
        # cross-referenced parts. The raw units archive out (scheduler), so the
        # topic note(s) ARE the memory.
        merged, all_tags = _consolidate_brief_sections(
            key, path, blocks, model_role, all_tags)
        return {"items": items, "base_stem": stem, "key": key, "tags": all_tags,
                "summarized": True,
                "parts": _brief_parts(key, merged, all_tags, title,
                                      sources=fold_sources)}

    body = None
    did_summarize = False
    if summarize:
        summary = _summarize_notes(blocks, model_role)   # SLOW
        if summary:
            body = f"# {title}\n\n{summary}"
            did_summarize = True
    if body is None:
        merged_prefix = ((existing_body + "\n\n---\n\n") if existing_body
                         else f"# {title}\n\n")
        body = _capped_merge(merged_prefix + "\n\n---\n\n".join(sections), title)

    if group_by in ("repo", "topic"):
        # No model: keep the OKR envelope, consolidation lives in the body
        # (Facts reset — they were folded in); Learnings survive verbatim.
        prev_learnings = _parse_brief(
            path.read_text(encoding="utf-8", errors="replace")
        )["learnings"] if path.exists() else []
        parts = [(stem, _render_brief(
            key, facts=[], body_md=re.sub(r"^#\s[^\n]*\n+", "", body.strip()),
            learnings=prev_learnings, title=title, tags=all_tags,
            sources=fold_sources))]
    else:
        parts = [(stem, _kind_frontmatter(title, stem, all_tags, len(items),
                                          did_summarize, fold_sources)
                  + body.strip() + "\n")]
    return {"items": items, "base_stem": stem, "key": key, "parts": parts,
            "tags": all_tags, "summarized": did_summarize}


def _retire_stale_parts(p: dict, archive, shutil) -> None:
    """Prior parts of this topic that the new (smaller) fold no longer produces
    — archived so a topic that shrank doesn't leave orphaned
    compacted-<key>-N.md files."""
    new_stems = {st for st, _ in p["parts"]}
    base = (p["base_stem"][len("compacted-"):]
            if p["base_stem"].startswith("compacted-") else p["base_stem"])
    for old in _brief_part_paths(base):
        if old.stem in new_stems:
            continue
        if re.match(rf"^{re.escape(p['base_stem'])}-\d+$", old.stem):
            try:
                shutil.move(str(old), str(archive / old.name))
            except Exception:  # noqa: BLE001
                pass


def _write_prepared(prepared: list, archive, archive_sources: bool,
                    out_files: list, summarized_files: list) -> int:
    """Write consolidated, THEN archive originals. Write-before-move: if a write
    fails the originals stay in place (no data loss) rather than being archived
    with no consolidated file. Returns how many originals moved."""
    import shutil
    moved = 0
    archive.mkdir(parents=True, exist_ok=True)
    for p in prepared:
        _retire_stale_parts(p, archive, shutil)
        if not _write_parts(p, out_files, summarized_files):
            continue
        if not archive_sources:
            # projection mode keeps raw units for the OTHER axis (a unit feeds
            # both its repo + topic brief).
            continue
        for d in p["items"]:
            try:
                shutil.move(str(d["_path"]), str(archive / d["file"]))
                moved += 1
            except Exception:  # noqa: BLE001
                pass
    return moved


def _write_parts(p: dict, out_files: list, summarized_files: list) -> bool:
    """Write this group's file part(s); True when at least one landed."""
    wrote_any = False
    for st, content in p["parts"]:
        fpath = _md_path_for_stem(st)
        try:
            fpath.write_text(content, encoding="utf-8")
        except Exception:  # noqa: BLE001 — keep originals; skip
            continue
        out_files.append(fpath.name)
        wrote_any = True
        if p["summarized"]:
            summarized_files.append(fpath.name)
    return wrote_any


def _ingest_brief(p: dict, st: str, group_by: str) -> None:
    fpath = _md_path_for_stem(st)
    doc = _parse(fpath)
    ingest_body = doc["body"]
    # Knowledge briefs (repo/topic) are OKR envelopes — ingest ONLY the
    # knowledge (Facts + body) so recall vectors don't carry the identical
    # Objective boilerplate every brief has.
    if group_by in ("repo", "topic"):
        try:
            from aiforge_core.runtime import work_notes
            ingest_body = work_notes.knowledge_text(doc["body"])
        except Exception:  # noqa: BLE001
            pass
    # Ingest the brief under its REAL scope so recall can reach it: a project
    # brief → its repo; the shared brief → 'shared' (global, surfaced for every
    # repo query); a topic brief → NULL (repo-agnostic, globally visible).
    # Burying every brief under 'notes' (the old default) made all consolidated
    # OKR knowledge invisible to repo-scoped recall.
    bkey = p.get("key")
    brepo = (None if group_by == "topic" else bkey) if bkey else "notes"
    # real kind ('knowledge') + clean human title (see ingest_dir)
    _ingest_unit(title=_brief_title(bkey or st), body=ingest_body,
                 kind="knowledge", tags=p["tags"], source=f"compacted:{st}",
                 repo=brepo, replace=True)


def _reingest_prepared(prepared: list, group_by: str, out_files: list) -> None:
    for p in prepared:
        for st, _ in p["parts"]:
            fpath = _md_path_for_stem(st)
            if fpath.name not in out_files or not fpath.exists():
                continue                   # write failed → don't ingest
            try:
                _ingest_brief(p, st, group_by)
            except Exception:  # noqa: BLE001
                pass


def _heal_after_compact(model_role: str, summarize: bool) -> tuple[dict, dict]:
    """Fold near-duplicate topics + repair data written before the scope/topic
    guards existed. Runs INSIDE compaction so the vocabulary self-heals every
    pass — there is no manual cleanup step. Bounded, so a compaction never
    becomes a migration."""
    merged: dict = {}
    try:
        from ._graph import merge_similar_topics
        merged = merge_similar_topics()
    except Exception as exc:  # noqa: BLE001 — a merge failure must never lose
        _log.debug("topic merge skipped: %s", exc)      # the compaction
    from . import _selfheal
    return merged, _selfheal.run_all(model_role=model_role,
                                     summarize=bool(summarize))


def compact(*, group_by: str = "kind", min_group: int = 2,
            dry_run: bool = False, summarize: bool = True,
            model_role: str = "learner", archive_sources: bool = True,
            force: bool = False, progress=None) -> dict:
    """Consolidate the sprawl of per-session ``.md`` memories into ONE
    standardized file per group, so the Memory folder stays legible.

    Grouping key (``group_by``): ``kind`` (default), ``tag``, or ``source``.
    Only groups with at least ``min_group`` files are compacted; singletons
    are left alone.

    ``summarize`` (default True): an available LLM (``model_role``'s primary →
    cloud chain) rewrites each group into a deduplicated, concise document, so
    the consolidated file stays SMALL instead of growing every run. On a
    re-compact the existing consolidated body is fed back in and re-summarised,
    keeping size bounded. If no model is reachable (or ``summarize=False``) it
    falls back to a deterministic merge (one ``## <title>`` section per note,
    appended). Originals are MOVED into ``<memory>/archive/<ts>/`` (reversible —
    never deleted) and the result is re-ingested into the searchable backend.

    ``dry_run`` returns the plan (group → file count) without touching disk.

    ``force`` ("compact at any cost"): re-consolidate EVERY existing brief too —
    not just scopes with new files. Each brief is re-read, re-chunked (chonkie)
    and re-summarised by the LLM from scratch, and singletons always fold
    (min_group→1, summarize→on). Use to rebuild the whole memory after a bad
    import, or to re-run the LLM pass over everything.
    """
    if force:
        summarize = True
        min_group = 1
    if dry_run:                      # read-only preview — no lock (don't wait
        planned = _gather_planned(   # behind a long-running compaction)
            group_by, min_group, model_role, force)
        return {"ok": True, "dry_run": True, "group_by": group_by,
                "groups": {k: len(v) for k, v in sorted(planned.items())},
                "files_in": sum(len(v) for v in planned.values()),
                "files_out": len(planned)}

    out_files: list[str] = []
    summarized_files: list[str] = []
    # Serialize compactions against each other so two concurrent runs can't read
    # the same stale consolidated state and clobber each other. Held across the
    # (slow) summarise, but it is NOT _WRITE_LOCK, so it does NOT block ordinary
    # chat-turn memory writes — only other compactions wait.
    with _COMPACT_LOCK:
        # Gather INSIDE the lock so a second compaction sees the first's result
        # (fresh sources + the just-written consolidated file as existing_body).
        planned = _gather_planned(group_by, min_group, model_role, force)
        if not planned:
            return {"ok": True, "dry_run": False, "group_by": group_by,
                    "groups": {}, "files_in": 0, "files_out": 0,
                    "note": "nothing to compact (no group ≥ min_group)"}
        archive = memory_dir() / "archive" / _now_iso().replace(":", "")
        total = len(planned)
        _log.info("compact[%s]: %d brief(s) to fold%s", group_by, total,
                  " via LLM" if (summarize and group_by in ("repo", "topic"))
                  else " (deterministic)")
        prepared: list[dict] = []
        for i, (key, items) in enumerate(sorted(planned.items()), 1):
            if progress:
                try:
                    progress(i, total, key)
                except Exception:  # noqa: BLE001
                    pass
            _log.info("compact[%s]: [%d/%d] folding '%s' (%d file%s)…",
                      group_by, i, total, key, len(items),
                      "" if len(items) == 1 else "s")
            prepared.append(_prepare_group(
                key, items, group_by=group_by, summarize=summarize,
                model_role=model_role, archive_sources=archive_sources))
        with _WRITE_LOCK:
            moved = _write_prepared(prepared, archive, archive_sources,
                                    out_files, summarized_files)
        _reingest_prepared(prepared, group_by, out_files)

    merged, healed = ({}, {})
    if group_by == "topic":
        merged, healed = _heal_after_compact(model_role, summarize)
    return {
        "ok": True, "dry_run": False, "group_by": group_by,
        "groups": {k: len(v) for k, v in sorted(planned.items())},
        "files_in": moved, "files_out": len(out_files),
        "compacted": out_files, "summarized": summarized_files,
        "merged_topics": merged.get("merged", 0),
        "selfheal": healed,
        "archive": str(archive),
    }


# Compacted files whose KEY is not a real topic — id-keyed briefs (chat run in
# a jira/confluence context / session scratch produced these) and per-kind
# blobs. Their knowledge is re-captured as topic units then the file archived,
# so a topic compaction re-folds them into meaningful topic briefs.
_CRYPTIC_KEY_RE = re.compile(
    r"^(?:\d{4,}|[a-z]{2,5}-\d+|session-\d+|"
    r"session|project|project-learning|learning|chat-summary|notes|compacted)$",
    re.IGNORECASE)


def _is_bug_artifact(fm: dict) -> bool:
    """A compacted-* file that is NOT a proper kind=knowledge brief (a stray
    kind=note unit written under a compacted name, or a source starting
    'agent:') — fold + archive it; the real topic brief is regenerated on the
    next compaction."""
    return bool((fm.get("kind") and fm.get("kind") != "knowledge")
                or str(fm.get("source") or "").startswith("agent:"))


def _is_live_split_part(base: str) -> bool:
    """A split overflow part (compacted-<topic>-N) is NOT stale when its topic's
    primary brief exists and that topic name isn't itself cryptic — e.g. keep
    compacted-auth-2.md (topic 'auth'), but still flag a truly cryptic
    compacted-clr-3049.md (no compacted-clr.md primary)."""
    m = re.match(r"^(.*)-\d+$", base)
    return bool(m and not _CRYPTIC_KEY_RE.match(m.group(1))
                and _resolve_md("compacted-" + m.group(1)) is not None)


def _stale_briefs() -> list:
    from aiforge_core.runtime import work_notes
    stale: list = []
    for pth in iter_briefs():
        try:
            fm = work_notes.parse_note(
                pth.read_text(encoding="utf-8", errors="replace"))["frontmatter"]
        except Exception:  # noqa: BLE001
            fm = {}
        if _is_bug_artifact(fm):
            stale.append(pth)
            continue
        base = pth.stem[len("compacted-"):]
        if _is_live_split_part(base):
            continue
        if _CRYPTIC_KEY_RE.match(base):
            stale.append(pth)
    return stale


def _facts_to_recapture(pth, parsed: dict) -> list:
    """The brief's Facts — or, for a legacy per-kind blob that keeps knowledge
    in the BODY rather than Facts, the body's knowledge lines."""
    from aiforge_core.runtime import work_notes
    facts = list(parsed["sections"].get("facts") or [])
    if facts:
        return facts
    body_know = work_notes.knowledge_text(
        pth.read_text(encoding="utf-8", errors="replace"))
    return [ln.lstrip("-* ").strip() for ln in body_know.splitlines()
            if ln.strip() and not ln.startswith("#")][:200]


def _fold_one_stale(pth, archive) -> tuple[int, bool]:
    """Re-capture the brief's facts as topic units and archive it (reversible).
    Returns ``(facts_moved, folded)``."""
    import shutil

    from aiforge_core.runtime import work_notes
    try:
        parsed = work_notes.parse_note(
            pth.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return 0, False
    moved = 0
    for f in _facts_to_recapture(pth, parsed):
        if not f.strip():
            continue
        try:
            capture("topic_learning", f.strip(), repo="notes",
                    source="cleanup:legacy-compacted")
            moved += 1
        except Exception:  # noqa: BLE001
            pass
    try:
        shutil.move(str(pth), str(archive / pth.name))
        return moved, True
    except Exception:  # noqa: BLE001
        return moved, False


def cleanup_legacy_compacted(*, dry_run: bool = False,
                             model_role: str = "learner",
                             refold: bool = True, progress=None) -> dict:
    """One-time tidy: fold id-keyed / per-kind ``compacted-*`` briefs back into
    the TOPIC axis. Each stale file's Facts are re-captured as topic units (no
    forced topic → the labeller re-clusters them), the original is archived
    (reversible), then a topic compaction re-folds everything into meaningful,
    tagged, split-aware topic briefs. ``dry_run`` reports the plan only."""
    stale = _stale_briefs()
    if dry_run:
        return {"ok": True, "dry_run": True,
                "stale": sorted(p.name for p in stale), "count": len(stale)}
    if not stale:
        _log.info("tidy-legacy: no cryptic/id-named briefs to fold")
        return {"ok": True, "dry_run": False, "folded": 0, "facts": 0,
                "note": "no id-keyed / per-kind compacted files to clean"}
    _log.info("tidy-legacy: folding %d cryptic/id-named brief(s)%s",
              len(stale), " + re-compacting" if refold else "")
    archive = memory_dir() / "archive" / ("cleanup-" + _now_iso().replace(":", ""))
    facts_moved = 0
    folded = 0
    with _COMPACT_LOCK:
        archive.mkdir(parents=True, exist_ok=True)
        for pth in stale:
            moved, ok = _fold_one_stale(pth, archive)
            facts_moved += moved
            folded += 1 if ok else 0
    # Re-fold the re-captured units into meaningful topic briefs — SKIPPED when
    # the caller (e.g. 'compact all') runs its own topic pass right after, so the
    # heavy LLM consolidation isn't done twice.
    topic = (compact(group_by="topic", min_group=1, summarize=True,
                     model_role=model_role, archive_sources=True,
                     progress=progress) if refold else None)
    return {"ok": True, "dry_run": False, "folded": folded,
            "facts": facts_moved, "archive": str(archive),
            "topic_compact": topic}
