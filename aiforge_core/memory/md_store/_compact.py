"""md_store internals: the ``compact()`` driver — preparing each topic group,
writing its parts, re-ingesting the briefs and repairing the store afterwards.
Capture sweeps, summarising, part rendering, archiving and legacy cleanup live
in the ``_compact_*`` modules and are re-exported here."""
from __future__ import annotations

import os
import re

from ._base import (
    _COMPACT_LOCK,
    _WRITE_LOCK,
    _brief_part_paths,
    _brief_title,
    _log,
    _md_path_for_stem,
    _now_iso,
    _parse,
    _slug,
    memory_dir,
)
from ._compact_archive import (  # noqa: F401  # re-exported
    _ARCHIVE_KEEP_DAYS,
    _ARCHIVE_STAMP_RE,
    _expired_archive_dirs,
    archive_covered_captures,
    prune_archive,
)
from ._compact_legacy import (  # noqa: F401  # re-exported
    _CRYPTIC_KEY_RE,
    _facts_to_recapture,
    _fold_one_stale,
    _is_bug_artifact,
    _is_live_split_part,
    _recapture_kind,
    _stale_briefs,
    cleanup_legacy_compacted,
)
from ._compact_parts import (  # noqa: F401  # re-exported
    _FOLD_FLOOR,
    _SOURCES_CAP,
    _absorb_part,
    _brief_parts,
    _consolidate_brief_content,
    _consolidate_brief_sections,
    _existing_brief_sections,
    _fold_sources,
    _kept_facts,
    _page_facts,
    _part_xref,
    _refold_content,
    _render_part,
    _union_back,
)
from ._compact_plan import (  # noqa: F401  # re-exported
    _add_existing_briefs,
    _apply_topic_floor,
    _brief_axis,
    _capped_merge,
    _explicit_topic,
    _gather_planned,
    _group_blocks,
    _label_topics,
    _live_capture_notes,
    _prior_brief_state,
)
from ._compact_summarize import (  # noqa: F401  # re-exported
    _COMPACT_BODY_CAP,
    _LABEL_LISTING_CAP,
    _NO_TOPIC,
    _SECTION_SEP,
    _SUMMARY_INPUT_CAP,
    _SUMMARY_OUT_TOKENS,
    _SUMMARY_SYS,
    _batch_under_cap,
    _group_key,
    _label_batches,
    _llm_topic_labels,
    _llm_topic_labels_once,
    _repo_key,
    _snap_known_topics,
    _split_to_cap,
    _summarize_block,
    _summarize_notes,
    _summary_input_cap,
    _topic_key,
    _topic_labels,
    _topic_split_cap,
)
from ._compact_sweep import (  # noqa: F401  # re-exported
    _CAPTURE_SIG_SUFFIX_RE,
    _demote_headings,
    _is_dead_brief,
    _retire_brief,
    sweep_empty_briefs,
    sweep_stale_captures,
)
from ._ingest import _ingest_unit
from ._render import brief_source_stems  # noqa: F401  # _compact_archive looks it up here


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
                # The fold ran for minutes with no lock held (by design), so the
                # write must re-check for facts captured meanwhile — see
                # _late_facts. Everything it needs to re-render is kept here.
                "sections": merged, "title": title, "sources": fold_sources,
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
        merged_prefix = ((existing_body + _SECTION_SEP) if existing_body
                         else f"# {title}\n\n")
        body = _capped_merge(merged_prefix + _SECTION_SEP.join(sections), title)

    if group_by in ("repo", "topic"):
        # No model: the new notes are merged into the PROSE body, and every
        # section the brief already had is carried forward untouched. Rendering
        # `facts=[]` here (what this used to do) deleted the whole consolidated
        # Facts list of every brief it touched — a boot with compaction off, or
        # with a ticket still in progress, wiped knowledge no fold could rebuild.
        existing, prev_tags = _existing_brief_sections(path, _slug(key))
        # The brief's own tags carry its axis (repo:<slug>), so they must
        # survive a fold that did not go through the model either.
        all_tags = sorted(set(list(prev_tags) + list(all_tags)))
        parts = _brief_parts(
            key, existing, all_tags, title, sources=fold_sources,
            body_md=re.sub(r"^#\s[^\n]*\n+", "", body.strip()))
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
        # Re-render FIRST (it can add a page back), then retire the parts the
        # final shape really doesn't have.
        _late_facts(p)
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


def _late_facts(p: dict) -> None:
    """Carry facts captured DURING the fold into the parts about to be written.

    ``_prepare_group`` reads the brief, then spends minutes in the LLM with no
    write lock held; a chat turn capturing a fact in that window wrote it to the
    same brief, and this write replaced it. Re-read under the caller's lock and
    re-render with anything new."""
    sections = p.get("sections")
    if not sections:
        return                        # prose/kind path: nothing to re-render
    base = _slug(p["key"])
    current, _tags = _existing_brief_sections(_md_path_for_stem(p["base_stem"]),
                                             base)
    have = {str(f) for f in sections.get("facts") or []}
    late = [f for f in (current.get("facts") or []) if str(f) not in have]
    if not late:
        return
    _log.info("compact: %d fact(s) captured during the fold of '%s' — kept",
              len(late), p["key"])
    merged = {**sections, "facts": list(sections.get("facts") or []) + late}
    p["sections"] = merged
    p["parts"] = _brief_parts(p["key"], merged, p["tags"], p.get("title") or "",
                              sources=p.get("sources"))


def _write_parts(p: dict, out_files: list, summarized_files: list) -> bool:
    """Write this group's file part(s); True when at least one landed."""
    from aiforge_core.config import _atomic
    wrote_any = False
    for st, content in p["parts"]:
        fpath = _md_path_for_stem(st)
        try:
            # Atomic: a brief half-written by a crash (or a reader catching the
            # truncated file) loses the whole fold, and this is the write that
            # replaces the ONLY copy of that knowledge.
            _atomic.write_text(str(fpath), content)
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
    from . import _topics
    bkey = p.get("key")
    brepo = _topics.brief_repo_scope(bkey, group_by, p.get("tags"))
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


def _auto_repair() -> dict:
    """Run the non-fact/duplicate repair pass unless it is switched off.

    Part of every compaction by design: the store is written to continuously by
    agents, so cleanup has to be automatic — a manual script only ever runs on
    the box someone remembered to run it on."""
    if os.environ.get("AIFORGE_MEMORY_AUTO_REPAIR", "1").strip().lower() in (
            "0", "off", "false", "no"):
        return {"skipped": "disabled"}
    try:
        from . import _repair
        return _repair.repair_captures()
    except Exception as exc:  # noqa: BLE001 — repair never breaks a compaction
        _log.debug("auto-repair skipped: %s", exc)
        return {"ok": False, "error": str(exc)}


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


def _report_progress(progress, i: int, total: int, key: str) -> None:
    if not progress:
        return
    try:
        progress(i, total, key)
    except Exception:  # noqa: BLE001 — a progress callback never fails a fold
        pass


def _fold_groups(planned: dict, o: dict) -> "tuple[int, bool]":
    """Fold every planned group, writing each as it is done → ``(moved,
    stopped)``. Split out of :func:`compact` so the pass reads as one loop;
    ``o`` carries the fold's settings and the accumulators it appends to."""
    moved, stopped = 0, False
    total = len(planned)
    for i, (key, items) in enumerate(sorted(planned.items()), 1):
        if o["skip_keys"] and key in o["skip_keys"]:
            continue
        if o["should_stop"] is not None and o["should_stop"]():
            stopped = True
            break
        _report_progress(o["progress"], i, total, key)
        _log.info("compact[%s]: [%d/%d] folding '%s' (%d file%s)…",
                  o["group_by"], i, total, key, len(items),
                  "" if len(items) == 1 else "s")
        prepared = [_prepare_group(
            key, items, group_by=o["group_by"], summarize=o["summarize"],
            model_role=o["model_role"], archive_sources=o["archive_sources"])]
        with _WRITE_LOCK:
            moved += _write_prepared(prepared, o["archive"],
                                     o["archive_sources"], o["out_files"],
                                     o["summarized_files"])
        _reingest_prepared(prepared, o["group_by"], o["out_files"])
        if o["on_group_done"] is not None:
            o["on_group_done"](key)
    return moved, stopped


def compact(*, group_by: str = "kind", min_group: int = 2,
            dry_run: bool = False, summarize: bool = True,
            model_role: str | None = None, archive_sources: bool = True,
            force: bool = False, progress=None, skip_keys=None,
            should_stop=None, on_group_done=None) -> dict:
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
    from . import _role
    # Consolidation is a judgement task (what is durable, what contradicts
    # what) → the memory/thinking role unless the caller pinned one.
    model_role = model_role or _role.memory_role()
    # SELF-REPAIR FIRST: retire captures that were never facts and collapse the
    # truncation ladders the old append-only writer left, so this pass does not
    # consolidate junk into a brief (where it is far harder to pick back out).
    repaired = _auto_repair()
    # Serialize compactions against each other so two concurrent runs can't read
    # the same stale consolidated state and clobber each other. Held across the
    # (slow) summarise, but it is NOT _WRITE_LOCK, so it does NOT block ordinary
    # chat-turn memory writes — only other compactions wait.
    with _COMPACT_LOCK:
        # Gather INSIDE the lock so a second compaction sees the first's result
        # (fresh sources + the just-written consolidated file as existing_body).
        planned = _gather_planned(group_by, min_group, model_role, force)
        if not planned:
            # Carries "repaired" like the success return: the repair pass ran
            # BEFORE the lock, and dropping its result exactly when there was
            # nothing to compact hid the one number this call produced.
            return {"ok": True, "dry_run": False, "group_by": group_by,
                    "groups": {}, "files_in": 0, "files_out": 0,
                    "repaired": repaired,
                    "note": "nothing to compact (no group ≥ min_group)"}
        archive = memory_dir() / "archive" / _now_iso().replace(":", "")
        total = len(planned)
        _log.info("compact[%s]: %d brief(s) to fold%s", group_by, total,
                  " via LLM" if (summarize and group_by in ("repo", "topic"))
                  else " (deterministic)")
        # Each group is written (and re-ingested) as soon as it is folded, not
        # all at the end: a pass that stops early — the idle compactor yields
        # the moment the user is back — keeps every group it finished, and
        # ``skip_keys`` lets the next pass resume after them.
        moved, stopped = _fold_groups(planned, {
            "group_by": group_by, "summarize": summarize,
            "model_role": model_role, "archive_sources": archive_sources,
            "archive": archive, "out_files": out_files,
            "summarized_files": summarized_files, "skip_keys": skip_keys,
            "should_stop": should_stop, "progress": progress,
            "on_group_done": on_group_done})

    merged, healed = ({}, {})
    if group_by == "topic" and not stopped:
        merged, healed = _heal_after_compact(model_role, summarize)
    return {
        "ok": True, "dry_run": False, "group_by": group_by, "repaired": repaired,
        "groups": {k: len(v) for k, v in sorted(planned.items())},
        "files_in": moved, "files_out": len(out_files),
        "compacted": out_files, "summarized": summarized_files,
        "merged_topics": merged.get("merged", 0),
        "selfheal": healed,
        "archive": str(archive),
        "stopped": stopped,
    }
