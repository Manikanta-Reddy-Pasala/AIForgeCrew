"""Merge duplicate library artifacts — rules, skills and workflows.

The library grows by ACCRETION. ``learn_skill`` fires the moment an agent
solves something hard, ``write_rule`` fires whenever a correction is captured,
and neither can see that "run the tests before pushing", "always run tests" and
"testing before push" are one instruction wearing three names. Nothing
overwrites anything (the writers key on a slug, so a near-miss name is a NEW
file), so the duplicates accumulate quietly and every one of them is prompt
overhead on turns that will never use it.

This module is the sweep that reconciles them:

  1. **Cluster deterministically, without an LLM.** Token similarity over
     name + description + triggers + the body head. A model is expensive and
     non-deterministic; deciding *which* artifacts are candidates is exactly
     the part that should be neither.
  2. **Merge with the shared client.** One ``structured_complete`` call per
     cluster at ``role=learner`` — the unattended maintenance role — so the
     pass sits under the operator's rate ceiling and shows up in the request
     meter like every other background sender. A merge pass that could
     out-shout interactive chat would be a worse bug than the duplicates.
  3. **Never destroy.** Members are copied into
     ``$AIFORGE_CONFIG_DIR/artifacts_archive/<kind>/`` before their files are
     removed, and the merged artifact records what it came from. A bad merge
     is a copy back, not a rewrite from memory.
  4. **Never re-decide.** Every cluster is fingerprinted by its members'
     content; a cluster already merged — or already skipped — is not sent to
     the model again until one of its members actually changes.

Bundled default playbooks are excluded on purpose: they ship with the product
and are re-seeded on upgrade, so merging them would fight the next release.

Switches (all optional):
  AIFORGE_ARTIFACT_MERGE=0        turn the sweep off entirely
  AIFORGE_MERGE_SIMILARITY=0.72   cluster threshold, 0..1
  AIFORGE_MERGE_MAX_PER_RUN=5     LLM merges per pass (cost ceiling)
  AIFORGE_MERGE_MAX_CLUSTER=6     members per cluster (prompt ceiling)
  AIFORGE_MERGE_HOUR=4            local hour for the scheduled pass
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

from ._artifact_apply import (  # noqa: F401  # re-exported
    _apply,
    _merged_extra,
    _provenance,
    _restore,
    archive,
    archive_dir,
)
from ._artifact_cluster import (  # noqa: F401  # re-exported
    _KIND_RANK,
    _SYSTEM,
    _coverage,
    _cross_kind_enabled,
    _Item,
    _llm_merge,
    _merge_prompt,
    _pairs_above,
    _response_model,
    _tokens,
    cluster_fingerprint,
    cross_kind_clusters,
    find_clusters,
    item_from,
    similarity,
    target_kind,
    too_large,
    validate_merge,
)

log = logging.getLogger("aiforge.artifact_merge")

# Guards the whole pass: the scheduled sweep and an operator's manual run must
# not archive-and-delete the same cluster concurrently.
_LOCK = threading.Lock()

KINDS = ("rules", "skills", "workflows")
_STATE_FILE = "artifact_merge_state.json"
_ARCHIVE_DIR = "artifacts_archive"
# How much of a body feeds the similarity tokens. The head carries the intent;
# the tail is usually examples, and two DIFFERENT rules that both end in a
# python snippet should not look alike because of it.
_BODY_HEAD = 600


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int, *, low: int = 0) -> int:
    try:
        return max(low, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def enabled() -> bool:
    """Off with AIFORGE_ARTIFACT_MERGE=0, or with jobs disabled wholesale."""
    if os.environ.get("AIFORGE_JOBS_DISABLE", "") in ("1", "true", "yes"):
        return False
    return os.environ.get("AIFORGE_ARTIFACT_MERGE", "1") not in (
        "0", "false", "no", "off")


def _rule_items() -> list[_Item]:
    from aiforge_core.runtime import repo_rules
    return [_Item("rules", r.name, r.description, tuple(r.triggers), r.body,
                  r.source, (("globs", tuple(r.globs)), ("always", r.always)))
            for r in repo_rules.load_global_rules()]


def _skill_items() -> list[_Item]:
    from aiforge_core.runtime import skills
    return [_Item("skills", s.name, s.description, tuple(s.triggers), s.body,
                  s.source) for s in skills.load()]


def _workflow_items() -> list[_Item]:
    from aiforge_core.runtime import workflows
    return [_Item("workflows", w.name, w.description, tuple(w.triggers),
                  w.body, w.source) for w in workflows.load()]


def _write_rule(item: _Item, links: list[str]) -> dict:
    from aiforge_core.runtime import repo_rules
    extra = dict(item.extra or ())
    return repo_rules.write_rule(
        item.name, item.body, description=item.description,
        triggers=list(item.triggers), links=links,
        globs=list(extra.get("globs") or []),
        always=bool(extra.get("always", True)))


def _write_skill(item: _Item, _links: list[str]) -> dict:
    from aiforge_core.runtime import skills
    return skills.write_skill(item.name, item.description, item.body,
                              list(item.triggers))


def _write_workflow(item: _Item, _links: list[str]) -> dict:
    from aiforge_core.runtime import workflows
    return workflows.write_workflow(item.name, item.description, item.body,
                                    list(item.triggers))


def _delete(kind: str, name: str) -> dict:
    if kind == "rules":
        from aiforge_core.runtime import repo_rules
        return repo_rules.delete_rule(name)
    if kind == "skills":
        from aiforge_core.runtime import skills
        return skills.delete_skill(name)
    from aiforge_core.runtime import workflows
    return workflows.delete_workflow(name)


_LOADERS = {"rules": _rule_items, "skills": _skill_items,
            "workflows": _workflow_items}
_WRITERS = {"rules": _write_rule, "skills": _write_skill,
            "workflows": _write_workflow}


def load(kind: str) -> list[_Item]:
    """Every artifact of ``kind`` this box would actually apply."""
    loader = _LOADERS.get(kind)
    return loader() if loader else []


# ── which ones may be touched ───────────────────────────────────────────────

def _builtin_names(kind: str) -> set[str]:
    """FILENAMES of the bundled playbooks. ``ensure_dirs`` copies them into the
    user's own dir keeping the filename, so a path check cannot tell a seeded
    default from something the operator wrote — the name still can."""
    try:
        from aiforge_core.runtime import workflows as _wf
        d = Path(_wf.__file__).resolve().parent / "builtin_playbooks" / kind
        return {f.name for f in d.glob("*.md")} if d.is_dir() else set()
    except Exception:  # noqa: BLE001 — classification must not break the sweep
        return set()


def _global_root(kind: str) -> Path | None:
    """The user-writable global dir for ``kind``, or None if it cannot be
    resolved (in which case nothing is treated as mergeable)."""
    try:
        if kind == "rules":
            from aiforge_core.runtime import repo_rules
            return repo_rules._global_rules_dir().resolve()
        if kind == "skills":
            from aiforge_core.runtime import skills
            return skills._global_dir().resolve()
        from aiforge_core.runtime import workflows
        return workflows._global_dir().resolve()
    except Exception:  # noqa: BLE001
        return None


def _has_scripts(item: _Item) -> bool:
    """A workflow with helper scripts next to it. The merge writes a NEW
    directory and cannot know which scripts the merged text still calls, so it
    leaves those alone rather than orphan an executable the body references."""
    if item.kind != "workflows":
        return False
    try:
        from aiforge_core.runtime import workflows
        return bool(workflows.scripts_for(item.source))
    except Exception:  # noqa: BLE001
        return True     # cannot tell → do not touch it


def mergeable(item: _Item) -> bool:
    """Whether the sweep may rewrite this artifact.

    Three exclusions, each for a different reason: a BUNDLED default is
    re-seeded on upgrade (merging it fights the next release), a REPO-local
    artifact belongs to that checkout and must not be folded into a global one,
    and a workflow carrying SCRIPTS would lose them."""
    if not item.source or not item.body.strip():
        return False
    if Path(item.source).name in _builtin_names(item.kind):
        return False
    if _has_scripts(item):
        return False
    root = _global_root(item.kind)
    if root is None:
        return False
    try:
        return root in Path(item.source).resolve().parents
    except OSError:
        return False


# ── state: never re-decide the same cluster ─────────────────────────────────

def _state_path() -> Path:
    return Path(str(config_dir())) / _STATE_FILE


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text()) or {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _atomic.write_text(_state_path(), json.dumps(state, indent=2))
    except OSError as exc:  # noqa: BLE001 — a lost state file costs a re-run
        log.warning("artifact_merge: state not saved: %s", exc)


def last_report() -> dict:
    return load_state().get("last_report") or {}


def _seen(state: dict, fp: str) -> bool:
    return fp in (state.get("decided") or {})


def _record(state: dict, fp: str, entry: dict) -> None:
    decided = dict(state.get("decided") or {})
    decided[fp] = entry
    state["decided"] = decided


# ── the pass ────────────────────────────────────────────────────────────────

def _merge_cluster(kind: str, cluster: list[_Item], state: dict,
                   dry_run: bool) -> dict:
    """One cluster: model → validate → apply. Returns the report row."""
    fp = cluster_fingerprint(cluster)
    names = [i.name for i in cluster]
    row = {"kind": kind, "fingerprint": fp, "members": names}
    oversize = too_large(cluster)
    if oversize:
        if not dry_run:
            _record(state, fp, {"action": "skipped", "reason": oversize,
                                "at": _now(), "kind": kind})
        return {**row, "action": "skipped", "reason": oversize}
    if dry_run:
        return {**row, "action": "would_merge"}
    try:
        merged = _llm_merge(kind, cluster)
    except Exception as exc:  # noqa: BLE001 — a model outage is not a failure
        log.warning("artifact_merge: %s merge failed: %s", kind, exc)
        return {**row, "action": "error", "error": str(exc)[:200]}
    why = validate_merge(merged, cluster)
    if why:
        _record(state, fp, {"action": "skipped", "reason": why,
                            "at": _now(), "kind": kind})
        return {**row, "action": "skipped", "reason": why}
    applied = _apply(kind, merged, cluster)
    if not applied["ok"]:
        return {**row, "action": "error", "error": applied["error"]}
    _record(state, fp, {"action": "merged", "into": merged.name.strip(),
                        "at": _now(), "kind": kind})
    return {**row, "action": "merged", "into": merged.name.strip(),
            "path": applied["path"], "archived": applied["archived"]}


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _pending(kind: str, state: dict, force: bool = False) -> list[list[_Item]]:
    """Clusters worth spending a model call on: never decided before, unless
    ``force`` (an operator asking again, e.g. after raising a cap)."""
    return [c for c in find_clusters(kind)
            if force or not _seen(state, cluster_fingerprint(c))]


def _per_kind_pairs(kinds, state, force):
    """``(kind, cluster)`` for the ordinary same-kind passes."""
    for kind in kinds:
        if kind not in KINDS:
            continue
        for cluster in _pending(kind, state, force):
            yield kind, cluster


def _cross_kind_pairs(kinds, state, force):
    """…then the ones no per-kind pass can see: the same instruction saved as a
    rule AND a skill. Drained last, so a plain duplicate is still collapsed
    within its own kind first and the cross-kind pass sees the tidied result —
    which is why this is a generator: nothing here reads the disk until the
    per-kind merges above have finished writing to it.
    """
    if not (_cross_kind_enabled() and set(kinds) >= set(KINDS)):
        return
    for cluster in cross_kind_clusters():
        if not force and _seen(state, cluster_fingerprint(cluster)):
            continue
        yield target_kind(cluster), cluster


def _collect(kinds, state, dry_run: bool, force: bool,
             budget: int) -> list[dict]:
    rows: list[dict] = []
    for pairs in (_per_kind_pairs(kinds, state, force),
                  _cross_kind_pairs(kinds, state, force)):
        for kind, cluster in pairs:
            # The budget is a COST ceiling, so it bounds model calls — a dry
            # run makes none and must show the operator every cluster, not the
            # first five of them.
            if not dry_run and len(rows) >= budget:
                return rows
            rows.append(_merge_cluster(kind, cluster, state, dry_run))
    return rows


def run(kinds: tuple[str, ...] | list[str] | None = None, *,
        dry_run: bool = False, limit: int | None = None,
        force: bool = False) -> dict:
    """Merge duplicates across ``kinds`` (default: all three).

    ``limit`` caps the LLM calls this pass makes — the nightly cost ceiling.
    ``dry_run`` reports the clusters and touches nothing, including the state,
    so a dry run never hides work from the pass that follows it. ``force``
    re-considers clusters already decided (what an operator wants after raising
    a cap; the nightly pass never sets it, or it would pay for the same verdict
    every night).
    """
    if not enabled():
        return {"ok": False, "error": "artifact merge disabled", "rows": []}
    # One sweep at a time. The nightly pass and an operator hitting Run in the
    # UI would otherwise archive-and-delete the same cluster twice, and the
    # second one would find its members already gone.
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "a merge pass is already running",
                "rows": []}
    try:
        state = load_state()
        budget = limit if limit is not None else _env_int(
            "AIFORGE_MERGE_MAX_PER_RUN", 5, low=1)
        rows = _collect(kinds or KINDS, state, dry_run, force, budget)
        report = {"at": _now(), "dry_run": bool(dry_run), "rows": rows,
                  "merged": sum(1 for r in rows if r["action"] == "merged")}
        if not dry_run:
            state["last_report"] = report
            _save_state(state)
        return {"ok": True, **report}
    finally:
        _LOCK.release()


def scheduled_pass() -> dict:
    """Entry point for the periodic scheduler (see api startup)."""
    try:
        out = run()
        if out.get("rows"):
            log.info("artifact_merge: %s merged, %s clusters seen",
                     out.get("merged"), len(out["rows"]))
        return out
    except Exception as exc:  # noqa: BLE001 — a sweep never kills the loop
        log.warning("artifact_merge: pass failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200], "rows": []}


__all__ = ["KINDS", "archive", "archive_dir", "cluster_fingerprint",
           "cross_kind_clusters", "enabled", "find_clusters", "last_report",
           "load", "load_state", "mergeable", "run", "scheduled_pass",
           "similarity", "target_kind", "too_large", "validate_merge"]
