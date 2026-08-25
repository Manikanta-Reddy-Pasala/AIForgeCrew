"""Unified, idempotent memory migrations — run once on API startup so EVERY
deployment auto-upgrades its old memory into the current scoped-OKR shape
without any manual step.

Chain (order matters):
  1. md_store.migrate_to_okr     — legacy flat brief format → OKR envelope
  2. okf.migrate_from_briefs     — compacted-<topic>.md briefs → OKR learnings
  3. okf.store.migrate_scoped    — flat okf/<type>/ → global/ + projects/<repo>/
  4. neo4j_drain                 — old Neo4j Observation/Decision nodes → md
                                   captures (which then roll up into 2+3)
  5. peers_out_of_okf            — okf/peers/<origin>/ → peers/<origin>/ (the
                                   two-tier compaction layout)

Steps 1 and 3 are cheap + safe to run every boot (they no-op once done). Steps 2
and 4 are ONE-SHOT — guarded by a marker file so a re-run can't undo later
curation (e.g. re-seeding briefs a user already reclassified). The marker lives
at ``<memory>/okr/.migrations.json``. All steps soft-fail; a migration never
blocks startup.
"""
from __future__ import annotations

import json
import logging
import os
import re

from aiforge_core.config import _atomic

log = logging.getLogger("aiforge.memory.migrations")


def _archive_okr_dag_folder() -> dict:
    """Move a live ``<memory>/okr/`` node-graph folder to a sibling
    ``<memory>/../memory-archive/okr[-N]/`` (kept, reversible). Idempotent —
    a no-op once the live dir is gone. Never raises."""
    import shutil
    try:
        from aiforge_core.memory.md_store import memory_dir
        src = memory_dir() / "okr"
        # A folder holding ONLY the migration bookkeeping marker (.migrations.json,
        # which _save_marker rewrites into okf_root after each archive) is NOT real
        # DAG data — treat it as empty so we don't re-archive a marker-only folder
        # into okr-1, okr-2… on every restart.
        if not src.is_dir():
            return {"skipped": "no live okr/ folder"}
        real = [p for p in src.iterdir() if p.name != ".migrations.json"]
        if not real:
            return {"skipped": "only migration marker — nothing to archive"}
        arch_root = memory_dir().parent / "memory-archive"
        arch_root.mkdir(parents=True, exist_ok=True)
        dest = arch_root / "okr"
        n = 1
        while dest.exists():                    # never clobber an earlier archive
            dest = arch_root / f"okr-{n}"
            n += 1
        shutil.move(str(src), str(dest))
        log.info("archived stale okr/ DAG folder → %s", dest)
        return {"ok": True, "archived_to": str(dest)}
    except Exception as exc:  # noqa: BLE001 — archiving must never break startup
        return {"ok": False, "error": str(exc)}


# Legacy frontmatter key → OKF name. created_at folds into timestamp too.
_OKF_KEY_RENAMES = (("kind", "type"), ("source_url", "resource"),
                    ("updated_at", "timestamp"), ("created_at", "timestamp"))


def _rewrite_file_frontmatter_to_okf(path) -> bool:
    """Rename legacy frontmatter keys → OKF names in ONE md file's frontmatter
    block (body untouched). Idempotent: a key is renamed only when its OKF name
    isn't already present, so re-runs and mixed files are safe. Atomic write.
    Returns True iff the file changed. Never raises."""
    import re
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    m = re.match(r"\A(---\s*\n)(.*?)(\n---\s*\n?)", text, re.DOTALL)
    if not m:
        return False
    head, fm, tail = m.group(1), m.group(2), m.group(3)
    lines = fm.split("\n")
    present = {ln.split(":", 1)[0].strip() for ln in lines if ":" in ln}
    changed = False
    out_lines: list[str] = []
    for ln in lines:
        renamed = False
        for legacy, okf in _OKF_KEY_RENAMES:
            mm = re.match(rf"^(\s*){re.escape(legacy)}(\s*:.*)$", ln)
            if mm and okf not in present:
                out_lines.append(f"{mm.group(1)}{okf}{mm.group(2)}")
                present.add(okf)
                changed = True
                renamed = True
                break
        if not renamed:
            out_lines.append(ln)
    if not changed:
        return False
    new_text = head + "\n".join(out_lines) + tail + text[m.end():]
    try:
        _atomic.write_text(path, new_text)
    except OSError:
        return False
    return True


def _migrate_frontmatter_to_okf() -> dict:
    """Rewrite legacy frontmatter keys (kind/source_url/updated_at/created_at)
    to OKF names across EVERY memory Markdown file — briefs (``compacted/``),
    raw captures (``captures/``), session notes, rule books, and the ``okf/``
    node bundle. Reserved OKF files (index.md/log.md) and the historical
    ``archive/`` snapshots are skipped. Idempotent + soft-fail — brings every
    pre-OKF on-disk file up to spec so a Google OKF reader consumes it directly."""
    try:
        from aiforge_core.memory.md_store import memory_dir
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    changed = 0
    scanned = 0
    root = memory_dir()
    if not root.is_dir():
        return {"ok": True, "rewritten": 0, "scanned": 0}
    for p in root.rglob("*.md"):
        # skip reserved OKF nav/audit files and archived historical snapshots
        if p.name in ("index.md", "log.md"):
            continue
        parts = set(p.relative_to(root).parts)
        if "archive" in parts or "memory-archive" in parts:
            continue
        scanned += 1
        try:
            if _rewrite_file_frontmatter_to_okf(p):
                changed += 1
        except Exception:  # noqa: BLE001 — one bad file never blocks the rest
            continue
    return {"ok": True, "rewritten": changed, "scanned": scanned}


def migrate_okf_format() -> dict:
    """Explicit, standalone OKF format migration (the ``./run.sh --migrate-okf``
    entry): rename a legacy ``okr/`` node bundle → ``okf/`` and rewrite every
    memory Markdown file's frontmatter to OKF names. Idempotent + soft-fail.
    run.sh calls this on every start so old-format files always converge to OKF."""
    out = {"dir_rename": _rename_okr_dir_to_okf(),
           "frontmatter": _migrate_frontmatter_to_okf()}
    log.info("migrate_okf_format: %s", out)
    return out


def _rename_okr_dir_to_okf() -> dict:
    """When the DAG is ON, rename a legacy ``<memory>/okr/`` node bundle to the
    OKF folder name ``<memory>/okf/`` so existing nodes are found at the new
    root. No-op if okr/ is absent or okf/ already exists. Never raises."""
    import shutil
    try:
        from aiforge_core.memory.md_store import memory_dir
        src = memory_dir() / "okr"
        dst = memory_dir() / "okf"
        if not src.is_dir():
            return {"skipped": "no legacy okr/ folder"}
        if dst.exists():
            # okf/ is already the live bundle — an okr/ holding only stale
            # bookkeeping (.migrations.json) is orphaned; remove it so the tree
            # is clean. Anything else stays put (don't clobber real data).
            leftover = [p for p in src.iterdir() if p.name != ".migrations.json"]
            if not leftover:
                shutil.rmtree(str(src), ignore_errors=True)
                return {"ok": True, "removed_stale_marker": str(src)}
            return {"skipped": "okf/ exists and okr/ has data — left in place"}
        shutil.move(str(src), str(dst))
        log.info("renamed legacy okr/ node bundle → okf/")
        return {"ok": True, "moved_to": str(dst)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _drain_peer_files(src, _paths, shutil) -> "tuple[int, int]":
    """Move each foreign node file out of ``src`` into the peers inbox. Returns
    ``(moved, kept)`` — a file already at the destination is kept in place (the
    newer layout wins, nothing is lost)."""
    moved = kept = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        dest = _paths.peers_root() / f.relative_to(src)
        if dest.exists():
            kept += 1                   # destination wins; nothing is lost
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        moved += 1
    return moved, kept


def _move_okf_peers_to_inbox() -> dict:
    """Move foreign OKF nodes out of ``okf/peers/<origin>/`` into the top-level
    ``peers/<origin>/`` inbox.

    ``okf/`` now means "knowledge this machine authored" — the compaction source
    and the only thing this peer contributes to the mesh — so other peers' raw
    nodes must not sit inside it. Idempotent; never clobbers a file already at the
    destination; never raises (a node must boot even when its memory tree is odd).
    """
    import shutil
    try:
        from aiforge_core.memory.sync import paths as _paths
        src = _paths.legacy_peers_dir()
        if not src.is_dir():
            return {"ok": True, "skipped": "no legacy okf/peers/ folder"}
        moved, kept = _drain_peer_files(src, _paths, shutil)
        for d in sorted(src.rglob("*"), reverse=True):
            if d.is_dir():
                _rmdir_if_empty(d)
        _rmdir_if_empty(src)                # gone entirely once fully drained
        log.info("moved %s foreign okf/peers/ node(s) → peers/ (%s kept in place)",
                 moved, kept)
        return {"ok": True, "moved": moved, "kept_at_destination": kept}
    except Exception as exc:  # noqa: BLE001 — a migration must never block startup
        return {"ok": False, "error": str(exc)}


def _rmdir_if_empty(d) -> None:
    import contextlib
    with contextlib.suppress(OSError):   # not empty, or already gone — both fine
        d.rmdir()


def _discover_repos() -> list:
    """Best-effort GENERIC repo list for classification — no hardcoded paths.
    Sources: AIFORGE_REPOS_ROOT (explicit), sibling git repos of the running
    checkout (repos are usually cloned side by side), and any repo context
    folders under the workspace. Returns real repo NAMES."""
    import subprocess
    repos: set = set()
    roots: list = []
    env_root = os.environ.get("AIFORGE_REPOS_ROOT", "").strip()
    if env_root:
        roots.append(env_root)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            roots.append(os.path.dirname(top.stdout.strip()))   # siblings
    except Exception:  # noqa: BLE001
        pass
    for rt in roots:
        try:
            for name in os.listdir(rt):
                if os.path.isdir(os.path.join(rt, name, ".git")):
                    repos.add(name)
        except OSError:
            continue
    return sorted(repos)


def _marker_path() -> str:
    from aiforge_core.memory.okf import store as _store
    return os.path.join(_store.okf_root(), ".migrations.json")


def _load_marker() -> dict:
    try:
        with open(_marker_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_marker(done: dict) -> None:
    import contextlib
    with contextlib.suppress(OSError):
        _atomic.write_text(_marker_path(), json.dumps(done))


_NEO4J_FACT_QUERY = (
    # ONLY durable MEMORY FACTS — NOT ingested repo content. Observation_v2 with
    # kind IN (code, doc) is the RAG search index (thousands of source-file
    # chunks); draining those would flood learnings with repo code. That index
    # is regenerable (reindex), so skip it.
    "MATCH (n) WHERE (n:Observation_v2 OR n:Decision_v2) "
    "AND coalesce(n.kind, '') NOT IN ['code', 'doc', 'chunk'] "
    "RETURN labels(n) AS labels, n.text AS text, n.repo AS repo, "
    "n.tags AS tags, n.topic AS topic LIMIT $lim")


def _neo4j_should_drain() -> bool:
    """Drain ONLY when the Neo4j backend is actually selected, or
    AIFORGE_MIGRATE_NEO4J=1 is set — so a normal embedded deploy never probes
    7687 (that was the connection-refused log spam)."""
    from aiforge_core.memory import backend_select
    forced = os.environ.get("AIFORGE_MIGRATE_NEO4J", "").strip() in (
        "1", "true", "yes")
    return backend_select.memory_backend() == "neo4j" or forced


def _topic_of(row) -> "str | None":
    """The row's topic — explicit ``n.topic`` or the first ``topic:`` tag."""
    tags = row.get("tags") or []
    return row.get("topic") or next(
        (t.split("topic:", 1)[1] for t in tags
         if isinstance(t, str) and t.startswith("topic:")), None)


def _capture_neo4j_row(row, md_store) -> bool:
    """Re-capture one drained fact via md_store. True when it was captured."""
    text = (row.get("text") or "").strip()
    if not text:
        return False
    is_dec = "Decision_v2" in (row.get("labels") or [])
    try:
        md_store.capture(
            "project_learning" if is_dec else "learning",
            ("DECISION: " + text) if is_dec else text,
            repo=row.get("repo") or "notes", topic=_topic_of(row),
            source="migrate:neo4j")
        return True
    except Exception:  # noqa: BLE001
        return False


def _neo4j_drain(limit: int = 5000) -> dict:
    """Best-effort ONE-SHOT export of old Neo4j memory into md captures, so a
    user who ran the Neo4j backend keeps their knowledge after switching to the
    embedded/OKR default. Reads Observation_v2 / Decision_v2 (text + repo +
    tags/topic) and re-captures each via md_store — which then rolls up into the
    briefs + OKR via the other steps. Soft-fail."""
    if not _neo4j_should_drain():
        return {"ok": True, "skipped": "neo4j not in use"}
    try:
        from neo4j import GraphDatabase

        from aiforge_core.memory import md_store
        from aiforge_core.memory.neo4j_conn import neo4j_params
        uri, user, pw = neo4j_params()
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001 — no driver / unreachable
        return {"ok": False, "error": f"connect: {exc}"}

    moved = 0
    try:
        with drv.session() as sess:
            for row in sess.run(_NEO4J_FACT_QUERY, lim=limit):
                moved += _capture_neo4j_row(row, md_store)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"read: {exc}", "moved": moved}
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "moved": moved}


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

    BUT: ``summarize=True, model_role="learner"`` is one LLM call per brief, and
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
        llm = mode == "always" or compact_window.open_now()
        r_repo = md_store.compact(group_by="repo", min_group=1, summarize=llm,
                                  model_role="learner", archive_sources=False)
        r_topic = md_store.compact(group_by="topic", min_group=1, summarize=llm,
                                   model_role="learner", archive_sources=True)
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
    if "neo4j_drain" not in done:
        r = _neo4j_drain()
        out["neo4j_drain"] = r
        # Mark done when it actually ran, and also when there was nothing to
        # drain (neo4j unused) — otherwise it retries forever.
        if r.get("skipped") or r.get("ok"):
            done.add("neo4j_drain")


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
    log.info("dedupe_all: okr=%s chat=%s", out.get("okr"), out.get("chat"))
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
    log.info("compact-all: [%d/%d] %s …", i, total, name)
    _notify_step(on_step, name, "run", None)
    try:
        out[name] = fn()
        if isinstance(out[name], dict) and out[name].get("ok") is False:
            log.error("compact-all: step %s reported failure: %s",
                      name, out[name].get("error") or out[name])
    except Exception as exc:  # noqa: BLE001
        out[name] = {"ok": False, "error": str(exc)}
        log.exception("compact-all: step %s CRASHED: %s", name, exc)
    log.info("compact-all: [%d/%d] %s done", i, total, name)
    _notify_step(on_step, name, "done", out[name])


def force_recompact_all(on_step=None) -> dict:
    """COMPACT ALL — redo EVERYTHING from scratch: tidy legacy/cryptic briefs,
    re-chunk (chonkie) + re-run the LLM over EVERY flat brief (not just new
    files), sweep stale captures, rebuild the OKR repo CARDS from learnings, and
    re-ingest into the search index. Heavy (full LLM pass); run on demand.
    Soft-fail per step. ``on_step(name, phase, result)`` is called at the start
    ('run') and end ('done') of each step for progress reporting."""
    from aiforge_core.memory import md_store

    # per-group sub-progress for the (slow, LLM-per-brief) compact steps →
    # surfaced through on_step so the UI shows 'topic 12/34' not a frozen 0/6.
    def _prog(name):
        def _cb(done, total, key):
            log.info("compact-all: %s %d/%d (%s)", name, done, total, key)
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
        ("tidy_legacy", lambda: md_store.cleanup_legacy_compacted(refold=False)),
        ("repo", lambda: md_store.compact(group_by="repo", force=True,
                                          model_role="learner", archive_sources=False,
                                          progress=_prog("repo"))),
        ("topic", lambda: md_store.compact(group_by="topic", force=True,
                                           model_role="learner", archive_sources=True,
                                           progress=_prog("topic"))),
        ("sweep", lambda: md_store.sweep_stale_captures(archive=True)),
        ("sweep_empty", lambda: md_store.sweep_empty_briefs(archive=True)),
        ("dedupe", dedupe_all),
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
    log.info("compact-all: START (%d steps)", len(steps))
    for i, (name, fn) in enumerate(steps, 1):
        _run_recompact_step(i, len(steps), name, fn, out, on_step)
    out["ok"] = True
    log.info("compact-all: DONE")
    return out


__all__ = ["run_startup_migrations", "purge_migrated_code",
           "force_recompact_all", "dedupe_all", "migrate_okf_format"]


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
    else:
        print(run_startup_migrations())
