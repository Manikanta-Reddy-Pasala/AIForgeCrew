"""Deduplicating and force-recompacting the whole store."""
from __future__ import annotations

import os


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.migrations as package
    return package


def dedupe_all() -> dict:
    """Remove duplicate OKR nodes AND duplicate chat sessions (from repeated /
    non-idempotent migrations). Soft-fail per side."""
    out: dict = {}
    if os.environ.get("AIFORGE_OKR_DAG", "0") == "1":
        try:
            from aiforge_core.memory.okf import store as _store
            out["okr"] = _store.dedupe_nodes()
        except Exception as exc:  # noqa: BLE001
            out["okr"] = {"ok": False, "error": str(exc)}
    try:
        from aiforge_core.runtime import chat_store
        out["chat"] = chat_store.dedupe_sessions()
    except Exception as exc:  # noqa: BLE001
        out["chat"] = {"ok": False, "error": str(exc)}
    _pkg().log.info("dedupe_all: okr=%s chat=%s", out.get("okr"), out.get("chat"))
    return out


def _notify_step(on_step, name: str, phase: str, result) -> None:
    """Fire the progress callback for one step boundary; never let a reporting
    error break the recompact."""
    if not on_step:
        return
    try:
        on_step(name, phase, result)
    except Exception:  # noqa: BLE001
        pass


def _run_recompact_step(i: int, total: int, name: str, fn, out: dict,
                        on_step) -> None:
    """Run one recompact step, recording its result (or crash) in ``out`` and
    bracketing it with 'run'/'done' progress callbacks. Soft-fail — a crashed
    step becomes ``{"ok": False, "error": …}`` and the rest still run."""
    _pkg().log.info("compact-all: [%d/%d] %s …", i, total, name)
    _notify_step(on_step, name, "run", None)
    try:
        out[name] = fn()
        if isinstance(out[name], dict) and out[name].get("ok") is False:
            _pkg().log.error("compact-all: step %s reported failure: %s",
                      name, out[name].get("error") or out[name])
    except Exception as exc:  # noqa: BLE001
        out[name] = {"ok": False, "error": str(exc)}
        _pkg().log.exception("compact-all: step %s CRASHED: %s", name, exc)
    _pkg().log.info("compact-all: [%d/%d] %s done", i, total, name)
    _notify_step(on_step, name, "done", out[name])


def _run_recompact_steps(steps, out: dict, on_step, checkpoint) -> bool:
    """Run the compact-all steps in order. True when the pass STOPPED early —
    the user came back, or a step yielded — so the caller reports it unfinished
    and the next idle window resumes from the checkpoint."""
    for i, (name, fn) in enumerate(steps, 1):
        if checkpoint is not None and checkpoint.step_done_already(name):
            continue
        if checkpoint is not None and checkpoint.should_stop():
            return True
        _pkg()._run_recompact_step(i, len(steps), name, fn, out, on_step)
        res = out.get(name)
        if isinstance(res, dict) and res.get("stopped"):
            return True
        # A step that FAILED is not done. Marking it done (what this used to do)
        # meant a model outage produced a cycle reported as complete, with the
        # steps that never ran skipped until the next one a day later.
        if checkpoint is not None and not (isinstance(res, dict)
                                           and res.get("ok") is False):
            checkpoint.step_done(name)
    return False


def force_recompact_all(on_step=None, checkpoint=None) -> dict:
    """COMPACT ALL — redo EVERYTHING from scratch: tidy legacy/cryptic briefs,
    re-chunk (chonkie) + re-run the LLM over EVERY flat brief (not just new
    files), sweep stale captures, rebuild the OKR repo CARDS from learnings, and
    re-ingest into the search index. Heavy (full LLM pass); run on demand.
    Soft-fail per step. ``on_step(name, phase, result)`` is called at the start
    ('run') and end ('done') of each step for progress reporting.

    ``checkpoint`` (the idle compactor's, runtime.compact_idle.Checkpoint) makes
    the pass RESUMABLE: steps and brief groups it already finished are skipped,
    and it stops between groups the moment ``checkpoint.should_stop()`` says the
    user is back — returning ``{"stopped": True}`` so the next idle window
    carries on from there. Without one (the Compact-all button) it runs whole."""
    from aiforge_core.memory import md_store

    def _resumable(axis):
        if checkpoint is None:
            return {}
        return {"skip_keys": checkpoint.groups_done(axis),
                "should_stop": checkpoint.should_stop,
                "on_group_done": lambda key: checkpoint.group_done(axis, key)}

    # per-group sub-progress for the (slow, LLM-per-brief) compact steps →
    # surfaced through on_step so the UI shows 'topic 12/34' not a frozen 0/6.
    def _prog(name):
        def _cb(done, total, key):
            _pkg().log.info("compact-all: %s %d/%d (%s)", name, done, total, key)
            if on_step:
                try:
                    on_step(name, "progress", {"done": done, "total": total, "key": key})
                except Exception:  # noqa: BLE001
                    pass
        return _cb

    out: dict = {}
    steps = [
        # fold cryptic/id-named files only — the topic step below does the heavy
        # LLM consolidation (with progress), so don't re-fold here (was the 600s
        # 'stuck at tidy_legacy').
        # FIRST: retire captures that were never facts (CLI fragments, headings,
        # raw chat turns) and collapse the truncation ladders the old
        # append-only writer left, so nothing below consolidates junk.
        ("repair", lambda: md_store.repair_captures()),
        ("tidy_legacy", lambda: md_store.cleanup_legacy_compacted(refold=False)),
        ("repo", lambda: md_store.compact(group_by="repo", force=True,
                                          model_role=md_store.memory_role(), archive_sources=False,
                                          progress=_prog("repo"), **_resumable("repo"))),
        ("topic", lambda: md_store.compact(group_by="topic", force=True,
                                           model_role=md_store.memory_role(), archive_sources=True,
                                           progress=_prog("topic"), **_resumable("topic"))),
        ("sweep", lambda: md_store.sweep_stale_captures(archive=True)),
        ("sweep_empty", lambda: md_store.sweep_empty_briefs(archive=True)),
        ("dedupe", _pkg().dedupe_all),
        # fold KIND-named junk briefs (compacted-learning.md, compacted-user-
        # comment.md …) — minted by the old topic kind-fallback — into the global
        # shared brief, then delete. Proper-named briefs only after this.
        ("fold_kind", lambda: md_store.fold_kind_briefs()),
        # merge near-duplicate TOPIC briefs (gpsd/gpsd-config/gpsd-configuration,
        # note/notes, gps/gpst) into one — kills topic-brief sprawl.
        ("merge_topics", lambda: md_store.merge_similar_topics()),
        # drop project/topic-brief facts that already live in the global brief
        # (recall unions global → those copies are pure redundancy).
        ("dedupe_global", lambda: md_store.dedupe_global_copies()),
        # cross-brief semantic cleanup: an LLM collapses duplicate/contradictory
        # facts that scattered across scope briefs (consolidate only dedupes
        # within a brief). Bounded — skips above a fact ceiling.
        ("reconcile", lambda: md_store.reconcile_briefs()),
        # CONTRADICTION-only cross-scope resolver — a new fact that contradicts a
        # repo OR the global brief REPLACES the stale one (recall unions repo ∪
        # global, so a contradiction misleads). Strict prompt, default ON (unlike
        # the aggressive dedup reconcile above). "Overwrite outdated, don't append."
        ("contradict", lambda: md_store.resolve_contradictions()),
        # dedupe_global / contradict can EMPTY a brief (its only fact moved to
        # the global scope or dropped as stale) — e.g. a global rule leaves an
        # empty topic stub. Sweep those NOW, before linking, so no empty brief
        # gets a link. (The earlier sweep runs before those steps.)
        ("sweep_empty_2", lambda: md_store.sweep_empty_briefs(archive=True)),
        # self-heal mis-scoped facts (move globals out of project briefs) — heavy
        # (LLM per fact), so opt-in via AIFORGE_OKR_REHEAL=1.
        ("reheal", lambda: md_store.reheal_scopes()
            if os.environ.get("AIFORGE_OKR_REHEAL", "0") == "1"
            else {"skipped": "disabled"}),
        # graph-health lint: strip dangling brief links (refs to deleted briefs);
        # reports orphans. Runs BEFORE map_scopes rewrites the link layer.
        ("lint_graph", lambda: md_store.lint_graph(repair=True)),
        # cross-scope mapping: link related briefs (project ↔ global ↔ topic)
        # AFTER they've settled (consolidated, deduped, empties swept, rehealed).
        ("map_scopes", lambda: md_store.map_scopes()),
        ("repo_profiles", lambda: (__import__(
            "aiforge_core.memory.okf.author", fromlist=["build_repo_profiles"]
        ).build_repo_profiles()
            if os.environ.get("AIFORGE_OKR_DAG", "0") == "1"
            else {"skipped": "okr-dag off"})),
        ("reingest", lambda: md_store.ingest_dir()),
    ]
    _pkg().log.info("compact-all: START (%d steps)", len(steps))
    if _run_recompact_steps(steps, out, on_step, checkpoint):
        return {**out, "ok": True, "stopped": True}
    out["ok"] = True
    # Which steps soft-failed, so a caller (the idle scheduler) can retry the
    # cycle instead of recording a pass that half ran as a success.
    out["failed_steps"] = [n for n, _ in steps
                           if isinstance(out.get(n), dict)
                           and out[n].get("ok") is False]
    _pkg().log.info("compact-all: DONE%s", (" (failed: " + ", ".join(out["failed_steps"])
                                     + ")") if out["failed_steps"] else "")
    return out

