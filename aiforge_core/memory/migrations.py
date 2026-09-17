"""Unified, idempotent memory migrations — run once on API startup so EVERY
deployment auto-upgrades its old memory into the current scoped-OKR shape
without any manual step.

Chain (order matters):
  1. md_store.migrate_to_okr     — legacy flat brief format → OKR envelope
  2. okf.migrate_from_briefs     — compacted-<topic>.md briefs → OKR learnings
  3. okf.store.migrate_scoped    — flat okf/<type>/ → global/ + projects/<repo>/
  4. peers_out_of_okf            — okf/peers/<origin>/ → peers/<origin>/ (the
                                   two-tier compaction layout)

Steps 1 and 3 are cheap + safe to run every boot (they no-op once done). Step 2
is ONE-SHOT — guarded by a marker file so a re-run can't undo later
curation (e.g. re-seeding briefs a user already reclassified). The marker lives
at ``<memory>/okr/.migrations.json``. All steps soft-fail; a migration never
blocks startup.
"""
from __future__ import annotations

import logging
import os
import re

from aiforge_core.config import _atomic  # noqa: F401  # tests patch migrations._atomic

from ._migrations_okf import (  # noqa: F401  # re-exported
    _OKF_KEY_RENAMES,
    _archive_okr_dag_folder,
    _discover_repos,
    _drain_peer_files,
    _load_marker,
    _marker_path,
    _migrate_frontmatter_to_okf,
    _move_okf_peers_to_inbox,
    _rename_okr_dir_to_okf,
    _rewrite_file_frontmatter_to_okf,
    _rmdir_if_empty,
    _save_marker,
    _split_frontmatter,
    migrate_okf_format,
)
from ._migrations_recompact import (  # noqa: F401  # re-exported
    _notify_step,
    _run_recompact_step,
    _run_recompact_steps,
    dedupe_all,
    force_recompact_all,
)

_MIGRATIONS_JSON = '.migrations.json'

log = logging.getLogger("aiforge.memory.migrations")



# A real learning is a sentence; a drained chunk is source code. These match the
# telltale code tokens; several hits (or one in a short body) flags a chunk.
_CODE_TOKEN_RE = re.compile(
    r"(?m)(^\s*(def |class |import |from \w+ import|public |private |func |"
    r"function |const |let |var |return |package |#include|@\w+)|[{};]\s*$|"
    r"=>|::|\bself\.|\bpublic static\b)")
_SHORT_CODE_RE = re.compile(r"(def |import |class |[{};])")


def _body_looks_like_code(body: str) -> bool:
    """True when an OKR learning body reads as source code, not prose."""
    hits = len(_CODE_TOKEN_RE.findall(body))
    return hits >= 3 or (hits >= 1 and len(body) < 240
                         and bool(_SHORT_CODE_RE.search(body)))


def _purge_drained_md(md_store) -> int:
    """Delete flat md files stamped ``source: migrate:neo4j``; return the count."""
    removed = 0
    for p in md_store.memory_dir().glob("*.md"):
        try:
            d = md_store._parse(p)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("source") or "") != "migrate:neo4j":
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _purge_code_learnings(okr_store, out: dict) -> None:
    """Remove OKR ``learning`` nodes whose body is source code; count the rest
    as kept. Prose learnings, solutions, repo cards, tasks, scripts stay."""
    import os as _os
    for d in okr_store.load_all():
        if d.get("type") != "learning":
            continue
        if not _body_looks_like_code(d.get("body") or ""):
            out["kept_learnings"] += 1
            continue
        try:
            _os.unlink(d["path"])
            out["removed_okr_learnings"] += 1
        except OSError:
            pass


def purge_migrated_code() -> dict:
    """Undo a buggy neo4j drain that captured repo CODE as learnings, WITHOUT
    touching real memory. Removes (1) flat md files stamped
    ``source: migrate:neo4j``, (2) OKR ``learning`` nodes whose body is clearly
    source code (not prose), then re-compacts briefs + rebuilds the index. Prose
    learnings, solutions, repo cards, tasks, scripts are KEPT. Soft-fail."""
    out = {"removed_md": 0, "removed_okr_learnings": 0, "kept_learnings": 0}
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.memory.okf import store as okr_store
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    out["removed_md"] = _purge_drained_md(md_store)
    _purge_code_learnings(okr_store, out)

    # rebuild: re-compact remaining md into clean briefs + refresh the index
    try:
        md_store.compact(group_by="topic", min_group=1, summarize=False,
                         archive_sources=True)
        md_store.sweep_stale_captures(archive=True)
        okr_store._invalidate()
        okr_store._write_index()
    except Exception:  # noqa: BLE001
        pass
    out["ok"] = True
    return out


def _step(out: dict, name: str, fn) -> bool:
    """Run one soft-fail migration step, recording its result under ``name``.
    Returns False when it RAISED (a step that returns ``ok: False`` still
    counts as having run — that is its own reported outcome)."""
    try:
        out[name] = fn()
        return True
    except Exception as exc:  # noqa: BLE001
        out[name] = {"ok": False, "error": str(exc)}
        return False


def _migrate_md_format() -> dict:
    from aiforge_core.memory import md_store
    return md_store.migrate_to_okr()


def _reembed_if_embedder_changed(out: dict) -> None:
    """RE-EMBED when the stored embeddings don't match the ACTIVE embedder — a
    backend/model switch or a migration that imported rows from a different
    embedder leaves mixed dims → broken KNN. Recomputes all embeddings with the
    current embedder + rebuilds the vec index (the same memory API the ingest
    path uses). No-op when dims already match. Embedded (SQLite) backend only."""
    try:
        from aiforge_core.memory import backend_select, sqlite_memory
        if backend_select.embedded() and (
                sqlite_memory.stored_dim_mismatch()
                or sqlite_memory.stored_embedder_changed()):
            log.info("migration: embedder changed (backend/model/dim) → "
                     "re-embedding all units")
            out["reembed"] = sqlite_memory.reembed_all()
    except Exception as exc:  # noqa: BLE001
        out["reembed"] = {"ok": False, "error": str(exc)}


def _move_files_into_folders(out: dict) -> None:
    """Move legacy root-level compacted-*.md briefs into compacted/ and the raw
    captures into captures/, so the memory-dir root holds only the compacted/ ·
    captures/ · archive/ folders and markers. Idempotent."""
    try:
        from aiforge_core.memory import md_store
        out["briefs_folder"] = md_store.migrate_briefs_to_folder()
        out["captures_folder"] = md_store.migrate_captures_to_folder()
    except Exception as exc:  # noqa: BLE001
        out["briefs_folder"] = {"ok": False, "error": str(exc)}


def _startup_compact(out: dict) -> None:
    """Fold old-format per-note .md files into their topic/repo briefs + retire
    masquerading captures NOW, so the brief→OKR step sees consolidated briefs.
    Idempotent; safe every boot.

    BUT: ``summarize=True, model_role=md_store.memory_role()`` is one LLM call per brief, and
    this runs on EVERY API boot — which on a laptop means every morning the lid
    opens. That is the same "compaction is running in my working day" intrusion
    the evening window exists to remove, arriving by a path the scheduler never
    sees. Outside the window the structural fold still runs (files move,
    captures are swept, the migration completes) with the model left out of it;
    the evening pass re-folds every brief through the learner anyway
    (force_recompact_all), so nothing is permanently un-summarised.
    AIFORGE_STARTUP_COMPACT=always restores the old boot-time LLM fold; =off
    skips this entirely.
    """
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import compact_window
        mode = (os.environ.get("AIFORGE_STARTUP_COMPACT", "window")
                .strip().lower())
        if mode in ("off", "0", "false", "no"):
            out["compact"] = {"skipped": "disabled"}
            return
        # Compaction is ENABLED BY DEFAULT now (the rate limiter caps it at
        # compaction_rpm). The one flag — compact_window.disabled() — still
        # short-circuits the boot fold's LLM calls when an operator turns it
        # off. Structural fold (file moves, capture sweep) still runs — only the
        # per-brief summarize call is suppressed.
        compaction_off = compact_window.disabled()
        llm = (not compaction_off) and (
            mode == "always" or compact_window.open_now())
        r_repo = md_store.compact(group_by="repo", min_group=1, summarize=llm,
                                  model_role=md_store.memory_role(), archive_sources=False)
        r_topic = md_store.compact(group_by="topic", min_group=1, summarize=llm,
                                   model_role=md_store.memory_role(), archive_sources=True)
        r_sweep = md_store.sweep_stale_captures(archive=True)
        out["compact"] = {"repo_in": r_repo.get("files_in"),
                          "topic_in": r_topic.get("files_in"),
                          "swept": r_sweep.get("swept"), "summarized": llm}
        if not llm:
            log.info("startup compaction: structural only — the learner "
                     "fold waits for the %02d:00 window",
                     compact_window.at_hour() or 0)
    except Exception as exc:  # noqa: BLE001
        out["compact"] = {"ok": False, "error": str(exc)}


def _one_shot_steps(out: dict, done: set) -> None:
    """Marker-guarded steps, so they can't undo later curation."""
    # Foreign nodes out of okf/ and into the top-level peers/ inbox. Nothing
    # writes okf/peers/ any more, so once it is drained there is no reason to
    # walk it again on every boot.
    if "peers_out_of_okf" not in done:
        r = _move_okf_peers_to_inbox()
        out["peers_out_of_okf"] = r
        if r.get("ok"):
            done.add("peers_out_of_okf")


def _dag_steps(out: dict, done: set) -> None:
    """The OKR-DAG (memory/okf/ node graph) steps — only when it is enabled."""
    from aiforge_core.memory.okf import author
    from aiforge_core.memory.okf import store as _store
    # Rename a legacy okr/ node bundle to the OKF folder name so its nodes are
    # found at okf_root().
    out["okr_to_okf_dir"] = _rename_okr_dir_to_okf()
    if "briefs_to_okr" not in done and _step(out, "briefs_to_okr",
                                             author.migrate_from_briefs):
        done.add("briefs_to_okr")
    # Scoped segregation moves what the above produced into global/projects.
    _step(out, "scoped", _store.migrate_scoped)
    # CLASSIFY: an LLM sorts the migrated GLOBAL learnings into their project
    # (or trashes noise). Deterministic tag/key parsing can't reliably tell a
    # repo brief from a topic brief; the LLM + repo-name match can. One-shot.
    if "classify" not in done:
        repos = _discover_repos()
        if not repos:
            # leave unmarked → retry next boot once repos are discoverable
            out["classify"] = {"skipped": "no repos discovered"}
        elif _step(out, "classify",
                   lambda: author.reclassify_global_learnings(repos)):
            done.add("classify")
    # Build the per-repo hub CARDS from each project's learnings.
    if "repo_profiles" not in done and _step(out, "repo_profiles",
                                             author.build_repo_profiles):
        done.add("repo_profiles")


def run_startup_migrations() -> dict:
    """Run the full idempotent migration chain. Called once per API boot; each
    step no-ops when nothing needs doing. Returns a per-step summary; never
    raises."""
    out: dict = {}
    marker = _load_marker()
    done = set(marker.get("done") or [])

    _reembed_if_embedder_changed(out)
    _move_files_into_folders(out)
    _startup_compact(out)
    # always-safe, idempotent steps
    _step(out, "format", _migrate_md_format)
    # Bring existing on-disk files to OKF v0.1: rename legacy frontmatter keys
    # (kind→type, source_url→resource, updated_at/created_at→timestamp) in the
    # briefs (+ okf/ nodes). Idempotent; makes pre-OKF files OKF-readable.
    _step(out, "okf_frontmatter", _migrate_frontmatter_to_okf)

    # OKR-DAG (the separate memory/okf/ node graph) is CONSOLIDATED OUT by
    # default — the flat compacted-<scope> briefs are the single OKR memory now.
    # Set AIFORGE_OKR_DAG=1 to re-enable the DAG build/migrate steps.
    if os.environ.get("AIFORGE_OKR_DAG", "0") == "1":
        try:
            _dag_steps(out, done)
        except Exception as exc:  # noqa: BLE001
            out["dag"] = {"ok": False, "error": str(exc)}
    else:
        # ARCHIVE any pre-existing okr/ folder OUT of the live memory dir (kept,
        # not deleted → reversible) so a stale node graph from an earlier build
        # can't shadow the flat briefs. Config-driven + idempotent.
        out["okr_archive"] = _archive_okr_dag_folder()
    _one_shot_steps(out, done)

    # Surface any step that failed — soft-fail steps otherwise swallow errors.
    for name, result in out.items():
        if isinstance(result, dict) and result.get("ok") is False:
            log.error("startup-migration: step '%s' FAILED: %s",
                      name, result.get("error") or result)
    _save_marker({"done": sorted(done), "version": 1})
    return out


if __name__ == "__main__":       # python -m aiforge_core.memory.migrations [flag]
    import sys
    if "--purge-code" in sys.argv:
        print(purge_migrated_code())
    elif "--dedupe" in sys.argv:              # remove duplicate OKR + chat
        _res = dedupe_all()
        # Node dedupe is local on every machine now (it only ever collapses
        # nodes this machine minted), so there is no role skip to explain.
        print(_res)
    elif "--recompact-all" in sys.argv:      # compact at any cost (+ dedupe)
        print(force_recompact_all())
    elif "--migrate-okf" in sys.argv:        # okr→okf dir + all md → OKF frontmatter
        print(migrate_okf_format())
    elif "--repair" in sys.argv:             # retire non-facts + collapse dupes
        from aiforge_core.memory import md_store as _md
        print(_md.repair_captures(dry_run="--dry-run" in sys.argv))
    else:
        print(run_startup_migrations())

__all__ = ["run_startup_migrations", "purge_migrated_code",
           "force_recompact_all", "dedupe_all", "migrate_okf_format"]
