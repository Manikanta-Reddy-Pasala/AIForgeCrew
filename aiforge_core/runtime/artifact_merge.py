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
import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

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


# ── the three kinds, behind one shape ───────────────────────────────────────

@dataclass(frozen=True)
class _Item:
    """One library artifact, flattened so the sweep never branches on kind.

    ``extra`` carries what only one kind has — a rule's ``globs`` and
    ``alwaysApply``. Dropping those on the way through would turn a merge into
    a silent scope change: two rules scoped to ``*.py`` come back applying to
    every turn, which is a bigger behaviour change than the duplication was.
    """

    kind: str
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    source: str
    extra: tuple = ()

    def fingerprint(self) -> str:
        h = hashlib.sha1(
            (self.name + "\x00" + self.body).encode("utf-8", "replace"))
        return h.hexdigest()[:16]


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


# ── clustering (deterministic, no model) ────────────────────────────────────

def _tokens(item: _Item) -> set[str]:
    from aiforge_core.runtime import skills as _sk
    text = " ".join([item.name, item.description, " ".join(item.triggers),
                     item.body[:_BODY_HEAD]])
    return _sk._tokens(text)


def similarity(a: _Item, b: _Item) -> float:
    """0..1 overlap between two artifacts.

    Delegates the token scoring to ``skills._fuzzy_overlap`` so "deploy" still
    matches "deployment" and there is ONE definition of what similar means —
    two copies of that rule is how search and this sweep would drift apart and
    start disagreeing about the same pair.
    """
    from aiforge_core.runtime import skills as _sk
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    # SYMMETRIC on purpose. ``_fuzzy_overlap`` scores one token set AGAINST
    # another and is directional — it answers "how much of a is in b", which
    # for a long rule and a short one differs by a lot in each direction. Used
    # raw, the cluster you get depends on which member the loop happened to
    # reach first, so "a≈b" and "b≉a" could both be true in the same pass.
    # Average both directions, each normalised by its own source.
    fwd = _sk._fuzzy_overlap(ta, tb) / float(len(ta))
    rev = _sk._fuzzy_overlap(tb, ta) / float(len(tb))
    return min(1.0, (fwd + rev) / 2.0)


def _pairs_above(items: list[_Item], threshold: float) -> list[tuple[int, int]]:
    return [(i, j)
            for i in range(len(items))
            for j in range(i + 1, len(items))
            if similarity(items[i], items[j]) >= threshold]


def find_clusters(kind: str, items: list[_Item] | None = None,
                  threshold: float | None = None) -> list[list[_Item]]:
    """Groups of near-duplicate artifacts, largest first.

    Union-find over the pairs above the threshold: "a≈b" and "b≈c" put all
    three in one cluster even when a and c only meet through b, which is how
    three spellings of one instruction actually accumulate.
    """
    pool = [i for i in (items if items is not None else load(kind))
            if mergeable(i)]
    if len(pool) < 2:
        return []
    thr = threshold if threshold is not None else _env_float(
        "AIFORGE_MERGE_SIMILARITY", 0.72)
    parent = list(range(len(pool)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in _pairs_above(pool, thr):
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[_Item]] = {}
    for idx, item in enumerate(pool):
        groups.setdefault(_find(idx), []).append(item)
    cap = _env_int("AIFORGE_MERGE_MAX_CLUSTER", 6, low=2)
    out = [sorted(g, key=lambda i: i.name)[:cap]
           for g in groups.values() if len(g) > 1]
    return sorted(out, key=len, reverse=True)


def cluster_fingerprint(cluster: list[_Item]) -> str:
    """Identity of a cluster BY CONTENT, so an unchanged cluster is never sent
    to the model twice — and a cluster whose member was edited is."""
    joined = "|".join(sorted(i.fingerprint() for i in cluster))
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


# ── the merge itself ────────────────────────────────────────────────────────

_SYSTEM = (
    "You consolidate duplicated agent instruction documents. You NEVER invent "
    "guidance and you NEVER drop a distinct instruction: the merged document "
    "must carry every rule, step, caveat and command that appears in any "
    "input. Where inputs conflict, keep the more specific one and say so in "
    "one clause. Output terse markdown — no preamble, no headings that only "
    "restate the title."
)


def _merge_prompt(kind: str, cluster: list[_Item]) -> str:
    parts = [f"These {len(cluster)} {kind} say substantially the same thing. "
             "Produce ONE that replaces all of them.\n"]
    for n, item in enumerate(cluster, 1):
        parts.append(
            f"\n--- {kind[:-1]} {n}: {item.name} ---\n"
            f"description: {item.description}\n"
            f"triggers: {', '.join(item.triggers) or '(none)'}\n"
            f"{item.body.strip()}\n")
    parts.append(
        "\nReturn the merged name (prefer the clearest existing one), a "
        "one-line description, the union of the triggers, and the merged "
        "body.")
    return "".join(parts)


def _response_model():
    """Built lazily: pydantic is a heavy import for a module that is mostly
    file walking, and the tests exercise the clustering without it."""
    from pydantic import BaseModel, Field

    class MergedArtifact(BaseModel):
        name: str = Field(description="the merged artifact's name")
        description: str = Field(default="", description="one line")
        triggers: list[str] = Field(default_factory=list)
        body: str = Field(description="merged markdown body")

    return MergedArtifact


def _llm_merge(kind: str, cluster: list[_Item]):
    from aiforge_core.llm.structured import structured_complete
    return structured_complete(
        "learner",
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": _merge_prompt(kind, cluster)}],
        _response_model(), max_tokens=1600, temperature=0.0)


def _coverage(merged_body: str, cluster: list[_Item]) -> float:
    """Fraction of the members' body vocabulary the merged body still carries.

    The failure this exists for is not a wrong merge, it is a LOSSY one: the
    model writes a tidy summary of five rules, three instructions quietly
    disappear, and the archive is the only place they still exist. Cheap, but
    it catches the summary-instead-of-merge case every time.
    """
    from aiforge_core.runtime import skills as _sk
    want: set[str] = set()
    for item in cluster:
        want |= _sk._tokens(item.body)
    if not want:
        return 1.0
    have = _sk._tokens(merged_body)
    return len(want & have) / float(len(want))


def too_large(cluster: list[_Item]) -> str:
    """"" unless the cluster cannot be sent whole.

    Truncating the INPUTS would be the worst of both worlds: the model merges
    what it was shown, the coverage check then fails against what it was not,
    and the cluster is recorded as a refusal forever. Skip it instead and say
    so — an operator can raise the cap or split the artifacts by hand.
    """
    total = sum(len(i.body) for i in cluster)
    cap = _env_int("AIFORGE_MERGE_MAX_CHARS", 12000, low=1000)
    if total > cap:
        return f"cluster is {total} chars, over the {cap}-char prompt cap"
    return ""


def validate_merge(merged, cluster: list[_Item]) -> str:
    """"" when the merge may be applied, else why it may not."""
    name = (getattr(merged, "name", "") or "").strip()
    body = (getattr(merged, "body", "") or "").strip()
    if not name or not body:
        return "model returned an empty name or body"
    # Against the LONGEST input, not the shortest. Merging a one-line rule with
    # a detailed one and keeping the one-liner IS the lossy case, and a floor
    # taken from the shortest member waves it straight through.
    longest = max(len(i.body.strip()) for i in cluster)
    if len(body) < longest * 0.8:
        return (f"merged body ({len(body)} chars) is shorter than the "
                f"longest input ({longest}) — a summary, not a merge")
    floor = _env_float("AIFORGE_MERGE_MIN_COVERAGE", 0.6)
    cov = _coverage(body, cluster)
    if cov < floor:
        return f"merged body covers only {cov:.0%} of the inputs' wording"
    return ""


# ── archive + apply ─────────────────────────────────────────────────────────

def archive_dir(kind: str) -> Path:
    return Path(str(config_dir())) / _ARCHIVE_DIR / kind


def archive(item: _Item) -> str:
    """Copy an artifact's file aside before it is removed. Returns the archive
    path, or "" when there was nothing on disk to keep."""
    src = Path(item.source)
    if not src.is_file():
        return ""
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    slug = src.stem if src.name.lower() not in ("skill.md", "workflow.md") \
        else src.parent.name
    dest = archive_dir(item.kind) / f"{slug}-{stamp}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return str(dest)


def _provenance(kind: str, merged_body: str, cluster: list[_Item]) -> str:
    names = ", ".join(sorted(i.name for i in cluster))
    return (merged_body.rstrip() + "\n\n<!-- merged from " + kind + ": "
            + names + " -->\n")


def _merged_extra(cluster: list[_Item]) -> tuple:
    """The kind-specific metadata the merged artifact inherits.

    For rules that is SCOPE, and it has to be the union: the merged rule must
    apply wherever any member applied, or the merge silently narrows what the
    agent is told. ``alwaysApply`` wins if ANY member had it (an always-on rule
    folded into a glob-scoped one would otherwise stop firing), and the globs
    are unioned for the same reason in the other direction — an empty glob list
    on an alwaysApply=False rule means it applies NOWHERE.
    """
    extras = [dict(i.extra or ()) for i in cluster if i.extra]
    if not extras:
        return ()
    globs: list[str] = []
    for e in extras:
        for g in e.get("globs") or ():
            if g not in globs:
                globs.append(g)
    return (("globs", tuple(globs)),
            ("always", any(bool(e.get("always")) for e in extras)))


def _restore(pairs: list[tuple[str, str]]) -> int:
    """Copy archived members back to where they came from. The sweep deletes
    BEFORE it writes (a merged artifact often reuses a member's name, and the
    writer would otherwise be undone by the delete), which means a failed write
    is the one moment the library is missing them. Restoring is what keeps that
    window from being data loss."""
    back = 0
    for src, dest in pairs:
        if not dest or not src:
            continue
        try:
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, src)
            back += 1
        except OSError as exc:
            log.error("artifact_merge: could not restore %s from %s: %s",
                      src, dest, exc)
    return back


def _apply(kind: str, merged, cluster: list[_Item]) -> dict:
    """Archive every member, remove them, then write the merged artifact.

    Order matters: a member whose name the merged artifact reuses must be gone
    (and archived) before the write, or the writer's overwrite would race the
    delete and take the merged file back out. A write that then fails rolls the
    members back from the archive.
    """
    pairs = [(i.source, archive(i)) for i in cluster]
    archived = [dest for _src, dest in pairs if dest]
    for item in cluster:
        try:
            _delete(item.kind, item.name)
        except Exception as exc:  # noqa: BLE001 — one bad unlink is not the run
            log.warning("artifact_merge: delete %s/%s failed: %s",
                        item.kind, item.name, exc)
    item = _Item(kind, merged.name.strip(),
                 (getattr(merged, "description", "") or "").strip(),
                 tuple(t.strip().lower()
                       for t in (getattr(merged, "triggers", []) or []) if t),
                 _provenance(kind, merged.body, cluster), "",
                 _merged_extra(cluster))
    links = [f"{kind[:-1]}:{i.name}" for i in cluster]
    try:
        res = _WRITERS[kind](item, links)
    except Exception as exc:  # noqa: BLE001 — a writer raising is still a loss
        res = {"ok": False, "error": str(exc)[:200]}
    if not res.get("ok"):
        restored = _restore(pairs)
        log.error("artifact_merge: %s write failed (%s) — restored %d member(s)",
                  kind, res.get("error"), restored)
        return {"ok": False, "path": "", "error": res.get("error", "write failed"),
                "archived": archived, "restored": restored}
    return {"ok": True, "path": res.get("path", ""), "error": "",
            "archived": archived}


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


def _collect(kinds, state, dry_run: bool, force: bool,
             budget: int) -> list[dict]:
    rows: list[dict] = []
    for kind in kinds:
        if kind not in KINDS:
            continue
        for cluster in _pending(kind, state, force):
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


__all__ = ["KINDS", "archive", "archive_dir", "cluster_fingerprint", "enabled",
           "find_clusters", "last_report", "load", "load_state", "mergeable",
           "run", "scheduled_pass", "similarity", "too_large", "validate_merge"]
