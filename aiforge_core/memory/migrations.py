"""Unified, idempotent memory migrations — run once on API startup so EVERY
deployment auto-upgrades its old memory into the current scoped-OKR shape
without any manual step.

Chain (order matters):
  1. md_store.migrate_to_okr     — legacy flat brief format → OKR envelope
  2. okr.migrate_from_briefs     — compacted-<topic>.md briefs → OKR learnings
  3. okr.store.migrate_scoped    — flat okr/<type>/ → global/ + projects/<repo>/
  4. neo4j_drain                 — old Neo4j Observation/Decision nodes → md
                                   captures (which then roll up into 2+3)

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

log = logging.getLogger("aiforge.memory.migrations")


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
    from aiforge_core.memory.okr import store as _store
    return os.path.join(_store.okr_root(), ".migrations.json")


def _load_marker() -> dict:
    try:
        with open(_marker_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_marker(done: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
        tmp = _marker_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(done, fh)
        os.replace(tmp, _marker_path())
    except OSError:
        pass


def _neo4j_drain(limit: int = 5000) -> dict:
    """Best-effort ONE-SHOT export of old Neo4j memory into md captures, so a
    user who ran the Neo4j backend keeps their knowledge after switching to the
    embedded/OKR default. Reads Observation_v2 / Decision_v2 (text + repo +
    tags/topic) and re-captures each via md_store — which then rolls up into the
    briefs + OKR via the other steps.

    ONLY attempts a connection when the Neo4j backend is actually selected, or
    AIFORGE_MIGRATE_NEO4J=1 is set — so a normal embedded deploy never probes
    7687 (that was the connection-refused log spam). Soft-fail."""
    from aiforge_core.memory import backend_select
    force = os.environ.get("AIFORGE_MIGRATE_NEO4J", "").strip() in ("1", "true", "yes")
    if backend_select.memory_backend() != "neo4j" and not force:
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
        with drv.session() as s:
            # ONLY durable MEMORY FACTS — NOT ingested repo content. Observation_v2
            # with kind IN (code, doc) is the RAG search index (thousands of
            # source-file chunks); draining those would flood learnings with repo
            # code. That index is regenerable (reindex), so skip it.
            rows = s.run(
                "MATCH (n) WHERE (n:Observation_v2 OR n:Decision_v2) "
                "AND coalesce(n.kind, '') NOT IN ['code', 'doc', 'chunk'] "
                "RETURN labels(n) AS labels, n.text AS text, n.repo AS repo, "
                "n.tags AS tags, n.topic AS topic LIMIT $lim", lim=limit)
            for r in rows:
                text = (r.get("text") or "").strip()
                if not text:
                    continue
                is_dec = "Decision_v2" in (r.get("labels") or [])
                tags = r.get("tags") or []
                topic = r.get("topic") or next(
                    (t.split("topic:", 1)[1] for t in tags
                     if isinstance(t, str) and t.startswith("topic:")), None)
                try:
                    md_store.capture(
                        "project_learning" if is_dec else "learning",
                        ("DECISION: " + text) if is_dec else text,
                        repo=r.get("repo") or "notes", topic=topic,
                        source="migrate:neo4j")
                    moved += 1
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"read: {exc}", "moved": moved}
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "moved": moved}


def run_startup_migrations() -> dict:
    """Run the full idempotent migration chain. Called once per API boot; each
    step no-ops when nothing needs doing. Returns a per-step summary; never
    raises."""
    out: dict = {}
    marker = _load_marker()
    done = set(marker.get("done") or [])

    # ── compact FIRST: fold old-format per-note .md files into their topic/repo
    # briefs + retire masquerading captures NOW (don't wait for the hourly job),
    # so the brief→OKR step below sees consolidated briefs. Same passes as the
    # hourly _compact_chat_md. Idempotent; safe every boot.
    try:
        from aiforge_core.memory import md_store
        r_repo = md_store.compact(group_by="repo", min_group=1, summarize=True,
                                  model_role="learner", archive_sources=False)
        r_topic = md_store.compact(group_by="topic", min_group=1, summarize=True,
                                   model_role="learner", archive_sources=True)
        r_sweep = md_store.sweep_stale_captures(archive=True)
        out["compact"] = {"repo_in": r_repo.get("files_in"),
                          "topic_in": r_topic.get("files_in"),
                          "swept": r_sweep.get("swept")}
    except Exception as exc:  # noqa: BLE001
        out["compact"] = {"ok": False, "error": str(exc)}

    # ── always-safe, idempotent steps ──────────────────────────────────────
    try:
        from aiforge_core.memory import md_store
        out["format"] = md_store.migrate_to_okr()
    except Exception as exc:  # noqa: BLE001
        out["format"] = {"ok": False, "error": str(exc)}

    # ── one-shot steps (marker-guarded so they can't undo later curation) ──
    if "briefs_to_okr" not in done:
        try:
            from aiforge_core.memory.okr import author
            out["briefs_to_okr"] = author.migrate_from_briefs()
            done.add("briefs_to_okr")
        except Exception as exc:  # noqa: BLE001
            out["briefs_to_okr"] = {"ok": False, "error": str(exc)}

    if "neo4j_drain" not in done:
        r = _neo4j_drain()
        out["neo4j_drain"] = r
        # only mark done when it actually ran (skipped = neo4j unused → retry
        # later if the user switches backends)
        if r.get("ok") and "skipped" not in r:
            done.add("neo4j_drain")
        elif r.get("skipped"):
            done.add("neo4j_drain")     # nothing to drain — don't retry forever

    # ── scoped segregation (moves what the above produced into global/projects)
    try:
        from aiforge_core.memory.okr import store as _store
        out["scoped"] = _store.migrate_scoped()
    except Exception as exc:  # noqa: BLE001
        out["scoped"] = {"ok": False, "error": str(exc)}

    # ── CLASSIFY: an LLM sorts the migrated GLOBAL learnings into their project
    # (or trashes noise). Deterministic tag/key parsing can't reliably tell a
    # repo brief from a topic brief; the LLM + repo-name match can. One-shot.
    if "classify" not in done:
        repos = _discover_repos()
        if repos:
            try:
                from aiforge_core.memory.okr import author
                out["classify"] = author.reclassify_global_learnings(repos)
                done.add("classify")
            except Exception as exc:  # noqa: BLE001
                out["classify"] = {"ok": False, "error": str(exc)}
        else:
            out["classify"] = {"skipped": "no repos discovered"}
            # leave unmarked → retry next boot once repos are discoverable

    # ── build the per-repo hub CARDS from each project's learnings (one-shot) ─
    if "repo_profiles" not in done:
        try:
            from aiforge_core.memory.okr import author
            out["repo_profiles"] = author.build_repo_profiles()
            done.add("repo_profiles")
        except Exception as exc:  # noqa: BLE001
            out["repo_profiles"] = {"ok": False, "error": str(exc)}

    _save_marker({"done": sorted(done), "version": 1})
    return out


__all__ = ["run_startup_migrations"]
