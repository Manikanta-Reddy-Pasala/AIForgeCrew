"""Two-tier knowledge compaction — the mesh fold, and the local view.

**Tier 1 — the admin, once for everybody.** Every spoke pushes what it authored
to the admin, where it lands in ``peers/<origin>/``. The admin folds that inbox
together with its own ``okf/`` into its own subtree of ``mesh/``: one node per
topic/repo group, each marked ``derived: mesh``. That result is what spokes pull.
One subtree per fold, keyed on the folding machine, so a role change leaves two
identities rather than one silently overwritten file — and so each machine
prunes only what it owns.

**Tier 2 — every machine, locally.** Each machine folds its own ``okf/``
together with the merged result into ``view/``, its working view. ``view/`` is
regenerated from scratch, never merged into, and is safe to delete at any
moment. It stays local because it is shaped by that machine's own context — and
because a synced view would amplify (see below).

Two rules break the amplification loop, and both are load-bearing:

* ``view/`` is not in ``paths.node_roots()``, so tier-2 output is never
  advertised and can never travel. Were it synced, the admin would fold it into
  ``mesh/``, it would come back down, and every round would re-merge knowledge
  that is already distilled — a drift that reads fine for days.
* Tier 1 ignores any input node carrying a ``derived`` marker. A peer that
  somehow republishes mesh content therefore cannot feed it back into the fold.

Neither tier owns a schedule: the sync cycle calls :func:`run_after_sync` once
per pass (``sync.loop.run_forever``). Both are skipped when their inputs are
unchanged — fingerprinted the way ``manifest.build()`` does it (file count,
total size, newest mtime), so an idle mesh costs a directory walk and no tokens.

Merging is ``work_notes.consolidate`` and grouping is ``md_store``'s — there is
no second copy of either here. Directory literals belong to ``sync.paths``.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from ._tiers_mesh import (  # noqa: F401  # re-exported
    _body,
    _claims,
    _facet,
    _fold,
    _group_of,
    _grouped,
    _mesh_dirs,
    _mesh_nodes,
    _node_id,
    _own_mesh_dir,
    _prune,
    _tier1_dirs,
    _trusted_origin,
    _unbulleted,
    _unrepresented,
    _view_dirs,
    _write,
)
from ._tiers_state import (  # noqa: F401  # re-exported
    _authored,
    _combine_fp,
    _derived,
    _fingerprint,
    _load,
    _origin,
    _read_state,
    _save_state,
    _state_path,
    _strip_edge_punct,
    _usable,
)

_MD = '**/*.md'

_log = logging.getLogger("aiforge.okf")

# Value of the `derived:` frontmatter marker on a tier-1 / tier-2 node. Marking
# both means "not authored here"; only the mesh marker is also a routing hint.
MESH = "mesh"
VIEW = "view"

# Where the two fingerprints live. A dotfile at the tree root: no manifest scan
# reaches it (they scan captures/, compacted/ and the node roots), so this
# machine's compaction bookkeeping never travels as if it were knowledge.
_STATE_FILE = ".tiers.json"

_ROLE = "learner"

# How much of a group name survives into its node id. The digest beside it, not
# the slug, is what keeps two groups apart, so this only has to stay readable —
# and short enough that the id clears ``paths.is_addressable``'s length cap.
_ID_SLUG_MAX = 48

# A rendered list marker at the start of a line. Bodies are re-folded, and OKR
# rendering already put these there: feeding them back in produced "- - fact".
_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def _run_tier(*, directory: Path, prefix: str, derived: str,
              inputs: list[dict], role: str) -> dict:
    """The half both tiers share: group, fold, render, write, prune."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    keep: set[str] = set()
    groups = _grouped(inputs)
    for group, items in sorted(groups.items()):
        node_id = _node_id(prefix, group)
        keep.add(node_id)
        tags = sorted({str(t) for n in items
                       for t in ((n.get("meta") or {}).get("tags") or [])})
        path = _write(directory, node_id, group,
                      _body(group, _fold(group, items, role), tags),
                      tags, derived)
        if path is not None:
            written.append(path.name)
    if len(keep) != len(groups):
        # Two groups sharing one id means one overwrote the other and its
        # knowledge is gone — invisible in the result, because `keep` held a
        # single id and the prune saw nothing missing. Loud beats silent.
        raise RuntimeError(
            f"{len(groups)} group(s) collapsed onto {len(keep)} node id(s)")
    return {"ok": True, "groups": len(keep), "written": written,
            "pruned": _prune(directory, keep)}


# ── tier 1 ────────────────────────────────────────────────────────────────

def distil_mesh(*, role: str = _ROLE) -> dict:
    """Fold authored knowledge into ``mesh/`` — once per group.

    Admin-only — the one step that is: the merge is LLM-expensive and
    non-deterministic, so two machines folding the same inbox produce two
    different answers. ``role.may_merge`` owns that policy *and* its soft-fail
    direction (OPEN — a machine with no admin configured IS the admin and must
    keep merging), so neither is restated here. Everything else about compaction
    stays local: see :func:`build_view` and ``md_store.compact``.

    A hub may serve several groups, and each is a separate tree with separate
    inputs: one fold reading them together would put one fleet's knowledge into
    another fleet's view. An admin with no groups folds its one tree exactly as
    before — that is the ungrouped deployment, and the loop degenerates to a
    single pass.
    """
    from aiforge_core.memory.sync import group as _group
    from aiforge_core.memory.sync import role as _role

    if not _role.may_merge():
        return {"ok": True, "skipped": "not-admin", "admin": _role.admin_id()}

    groups = _group.known()
    if not groups:
        return _distil_one(role=role)
    return _distil_each(groups, role)


def _distil_each(groups: list[str], role: str) -> dict:
    """One fold per group, each inside that group's scope.

    A fold that dies takes its own group's cycle and never the hub: the other
    groups' knowledge is unrelated, and one bad tree must not stop every other
    fleet converging.
    """
    from aiforge_core.memory.sync import group as _group

    out: dict = {"ok": True, "groups": {}}
    for name in groups:
        try:
            with _group.scoped(name):
                out["groups"][name] = _distil_one(role=role)
        except Exception as exc:  # noqa: BLE001 — one bad group is not the rest
            _log.warning("tiers: mesh fold failed for group %s: %s", name, exc)
            out["groups"][name] = {"ok": False, "error": str(exc)[:200]}
    return out


def _distil_one(*, role: str = _ROLE) -> dict:
    """The fold itself, against whichever tree ``_io.root()`` currently names.

    The admin-only check is the caller's (``distil_mesh``): re-checking it here
    would run once per group and answer the same thing every time.
    """
    from aiforge_core.memory.sync import _io, paths, snapshot

    sources = (paths.okf_dir(), paths.peers_root())
    if _read_state().get("mesh") == _fingerprint(_tier1_dirs()):
        return {"ok": True, "skipped": "unchanged"}

    # Snapshot the INPUT fingerprint BEFORE reading the inputs — the same
    # before-the-fold ordering build_view uses. The stamp saved below combines
    # THIS snapshot with a post-fold read of mesh/, never one post-fold read of
    # everything. WHY: the old code re-computed _fingerprint(_tier1_dirs()) AFTER
    # the fold, so any node that landed in okf/ or peers/ between _load and
    # _save_state was baked into the "fresh" stamp without ever being folded — it
    # stayed on disk, stayed advertised, and reached no peer's view/ until some
    # unrelated file changed. Freezing the inputs here means such an arrival
    # leaves the stamp describing a tree that no longer matches, so the next
    # cycle re-folds and picks it up. mesh/ is still read post-fold (there is no
    # race on what we ourselves just wrote), which keeps _tier1_dirs' repair of a
    # mesh destroyed from outside.
    inputs_fp = _fingerprint(sources)

    def _stamp() -> list:
        return _combine_fp(inputs_fp, _fingerprint((paths.mesh_dir(),)))

    inputs = _usable(_authored(_load(sources)))
    if not inputs:
        # Nothing authored anywhere. Returning before the fold also means the
        # prune never runs: an admin that momentarily reads an empty tree must
        # not answer by deleting the mesh everyone else is using.
        _save_state("mesh", _stamp())
        return {"ok": True, "skipped": "no-inputs", "inputs": 0}
    _log.info("tiers: mesh fold over %d authored node(s)", len(inputs))
    # A revert point, taken before the fold replaces it. Hardlinked, so this
    # costs inodes rather than bytes — which is what makes "before every fold"
    # affordable, and affordability is what makes the snapshot exist at all.
    snapshot.take(_io.root())
    result = _run_tier(directory=_own_mesh_dir(), prefix="M", derived=MESH,
                       inputs=inputs, role=role)
    # A run that dies half way records nothing and re-reads its inputs next cycle.
    _save_state("mesh", _stamp())
    return {**result, "inputs": len(inputs)}


# ── tier 2 ────────────────────────────────────────────────────────────────

def build_view(*, role: str = _ROLE) -> dict:
    """Rebuild ``view/`` from this machine's ``okf/`` plus the merged result.

    Runs on EVERY machine, admin included. It is the cheap half — its input is
    one machine's own knowledge plus a mesh that is already distilled — and its
    output is shaped by that machine's own context, which is the whole reason it
    is not centralised.

    Skipped unless one of its inputs — the mesh or our own ``okf/`` — actually
    changed, so a cycle where nothing arrived and nothing was authored costs no
    tokens. A mesh that is missing, empty or unreadable leaves the previous view
    exactly where it is: a bad mesh must never destroy a good local view.
    """
    from aiforge_core.memory.sync import paths

    fingerprint = _fingerprint(_view_dirs())
    if _read_state().get("view") == fingerprint:
        return {"ok": True, "skipped": "unchanged"}

    mesh = _usable(_mesh_nodes())
    if not mesh:
        _log.info("tiers: no usable mesh content — keeping the existing view")
        return {"ok": True, "skipped": "no-mesh"}

    # Only what the mesh does not already carry: tier 1 folded this machine's
    # okf/ in already, so passing all of it would merge the same facts twice.
    inputs = mesh + _unrepresented(_usable(_load((paths.okf_dir(),))), mesh)
    _log.info("tiers: view rebuild over %d node(s)", len(inputs))
    result = _build_view_atomically(inputs, role)
    if not result.get("ok", True):
        # A failed build leaves the previous view exactly where it was, so the
        # fingerprint must NOT advance: the next cycle has to try again.
        return result
    _save_state("view", fingerprint)
    return {**result, "inputs": len(inputs)}


def _build_view_atomically(inputs: list[dict], role: str) -> dict:
    """Build the view into a sibling directory and swap it in.

    Never in place. ``view/`` is the working knowledge every agent reads, and an
    in-place rebuild that dies part-way — a crash, an ENOSPC, a learner that
    stops answering — left it half old and half new, which is strictly worse
    than yesterday's view intact. The swap is two renames on one filesystem, so
    there is no window where ``view/`` is missing for longer than a rename.

    ``view.tmp`` and ``view.old`` sit BESIDE ``view/``, which is itself absent
    from ``paths.node_roots()``, so neither is ever advertised to a peer.
    """
    from aiforge_core.memory.sync import paths

    final = paths.view_dir()
    staging = final.parent / "view.tmp"
    previous = final.parent / "view.old"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        result = _run_tier(directory=staging, prefix="V", derived=VIEW,
                           inputs=inputs, role=role)
    except Exception as exc:  # noqa: BLE001 — a failed build keeps the old view
        shutil.rmtree(staging, ignore_errors=True)
        _log.warning("tiers: view build failed, keeping the previous view: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}

    shutil.rmtree(previous, ignore_errors=True)
    if final.exists():
        final.rename(previous)
    staging.rename(final)
    shutil.rmtree(previous, ignore_errors=True)
    return result


# ── the read side: what agents get from tier 2 ────────────────────────────

def view_nodes() -> list[dict]:
    """The working view, parsed — the only way retrieval reaches folded
    knowledge (spec §"What agents read": ``okf/`` plus ``view/``).

    ``peers/`` is deliberately absent: it is an input to the fold, and reading
    it here as well would surface the same content twice — once raw and once
    distilled — in the agent's context.

    ``mesh/`` is the fallback, and only ever a fallback: a machine that has
    pulled a fresh merge but not yet folded it would otherwise read purely local
    memory while a perfectly good merge sat on disk. Once ``view/`` exists this
    never fires — which matters, because the two must not both be read at once:
    ``view/`` IS the mesh folded with our own notes, so returning both would
    double every fact.
    """
    from aiforge_core.memory.sync import paths

    view = _usable(_load((paths.view_dir(),)))
    return view if view else _usable(_mesh_nodes())


def unrepresented(local: list[dict], view: list[dict]) -> list[dict]:
    """``local`` nodes the view does not already carry.

    The recall-side half of the no-double-surfacing rule, and the same
    comparison tier 2 uses to pick its own inputs: a node whose every claim is
    already inside the fold would otherwise be rendered twice into one prompt.
    """
    return _unrepresented(local, view) if view else list(local)


# ── retiring a demoted admin's own fold ───────────────────────────────────

def _retire_own_mesh() -> dict:
    """Tombstone this machine's own ``mesh/<id>/`` fold once it is not the admin.

    A fold is a class B node like any other: advertised, replicated, and keyed
    on its minting origin (``mesh/<origin>/``). So the subtree of a machine that
    used to be the admin otherwise rides every future sync out to every spoke
    and every NEW spoke, forever — one dead subtree per role change — and the
    tier-1 prune never reaches it (``_prune`` only ever touches the *current*
    fold's own dir, and ``_mesh_nodes`` ignores non-admin origins).

    Nobody else can clean it up: a foreign mesh node deleted locally is
    re-fetched on the next pull, and forging a tombstone for another origin is
    exactly what ``apply._accept_class_b`` refuses (it would delete that
    machine's nodes everywhere). The retiring owner is the only one allowed to
    remove its own identity — and it does so through the self-origin-guarded
    ``tombstone.mark_deleted``, whose tombstone propagates the removal instead of
    letting the next pull bounce the node back.

    A machine that is switched off at the moment it is demoted cannot run this,
    so its subtree lingers until it comes back and retires — the unavoidable
    price of never forging somebody else's deletion.

    **Retirement needs a SUCCESSOR, not merely the absence of our own role.**
    Being a spoke is not enough: a box can lose the role by accident — a service
    unit that restarts ``run.sh`` without ``--admin``, an env file edited by
    hand — and deleting the fleet's merged knowledge because of a missing flag
    is not recoverable by putting the flag back, since the tombstones propagate
    to every spoke on its next pull. So we retire only once another machine is
    actually known to be the admin (``role.admin_id()``, learned from its
    manifest), and never while that answer is empty or is still us. A stale
    subtree is untidy; a deleted one is gone.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import identity, merge, paths, tombstone
    from aiforge_core.memory.sync import role as _role

    me = paths.fold(identity.self_id())
    try:
        if _role.is_admin():
            return {"retired": 0, "skipped": "still-admin"}
        successor = _role.admin_id()
    except Exception as exc:  # noqa: BLE001 — unsure of the role → never delete a fold
        _log.info("okf: cannot resolve the role (%s) — keeping our mesh fold", exc)
        return {"retired": 0, "skipped": "no-role"}
    if not successor or successor == me:
        # A spoke that has never reached its admin (or has none configured) has
        # no evidence anybody else is folding. See the docstring.
        _log.info("okf: no other machine is known to be the admin — keeping our "
                  "mesh fold rather than deleting it")
        return {"retired": 0, "skipped": "no-successor"}

    own = _own_mesh_dir()
    if not own.is_dir():
        return {"retired": 0}

    retired = 0
    for p in sorted(own.glob("*.md")):
        key = p.stem
        rev = 0
        try:
            meta = (nodes.parse_node(p.read_text(encoding="utf-8")).get("meta") or {})
            rev = merge.as_rev(meta.get("rev"))
        except OSError:      # unreadable is still deletable; rev 0 tombstones it
            pass
        try:
            p.unlink()
        except OSError as exc:
            _log.warning("okf: could not drop stale mesh node %s (%s)", p, exc)
            continue
        # Tombstone AFTER the unlink: mark_deleted refuses while a copy is still
        # on disk (a per-scope id can legitimately name a live node elsewhere),
        # so the removal must come first — the same contract okf.author and
        # store.dedupe_nodes honour when they hand it their already-removed node.
        tombstone.mark_deleted(identity.self_id(), key, rev)
        retired += 1

    try:
        own.rmdir()          # tidy the now-empty subtree; harmless if not empty
    except OSError:
        pass
    if retired:
        _log.info("okf: retired %d stale mesh node(s) after ceasing to be the "
                  "admin", retired)
    return {"retired": retired}


# ── the one entry point the sync cycle calls ──────────────────────────────

def disabled() -> bool:
    """True when OKF compaction is turned OFF for this machine.

    ``AIFORGE_COMPACT_DISABLE`` is the wrong switch for this: it stops the LLM
    *fold* (``work_notes.consolidate`` degrades to a deterministic merge) while
    every other part of the pass — the brief→node conversion, both tier walks,
    the rewrites and the rev bumps they cause — keeps running and keeps pushing.
    That is the right default for a machine that merely wants a quiet working
    day; it is not an off switch for OKF itself.

    This one is, and it is read per call rather than captured at import so that
    flipping the flag takes effect on the NEXT cycle of an already-running
    ``sync.loop`` — the daemon is supervised and long-lived, and a restart to
    change a toggle is what made the last operator edit ``.env`` and bounce the
    whole stack.
    """
    return os.environ.get("AIFORGE_OKF_DISABLE", "0").strip().lower() in (
        "1", "true", "yes")


def run_after_sync(*, role: str = _ROLE) -> dict:
    """Both tiers, once, after a sync pass — so this cycle's arrivals are in.

    Each step soft-fails independently: compaction is upkeep, and a fold that
    raises must cost a cycle rather than the daemon that would have retried it.

    Retirement runs first: a machine that is no longer the admin must retract
    its own now-stale mesh fold before anything else, or it stays advertised
    forever (see :func:`_retire_own_mesh`). It runs unconditionally, so a demoted
    machine whose inputs are otherwise unchanged still retracts.

    Then the brief→node conversion, before either tier: briefs are local files
    that never travel, so a fact only reaches the other machines once it is an
    OKF node (``okf.author.sync_briefs_to_nodes``). Running it here means this
    cycle's own compaction output is in ``okf/`` in time for this cycle's fold
    and the next push.
    """
    if disabled():
        _log.info("okf: compaction OFF (AIFORGE_OKF_DISABLE) — no fold, no "
                  "brief conversion, no tier walk this cycle")
        return {"ok": True, "disabled": True}
    out: dict = {}
    try:
        out["retire"] = _retire_own_mesh()
    except Exception as exc:  # noqa: BLE001 — see docstring: never kill the loop
        _log.warning("tiers: mesh retirement failed (%s)", exc)
        out["retire"] = {"ok": False, "error": str(exc)}
    try:
        from aiforge_core.memory.okf import author

        out["briefs"] = author.sync_briefs_to_nodes()
    except Exception as exc:  # noqa: BLE001 — see docstring: never kill the loop
        _log.warning("tiers: brief→node conversion failed (%s)", exc)
        out["briefs"] = {"ok": False, "error": str(exc)}
    for name, fn in (("mesh", distil_mesh), ("view", build_view)):
        try:
            out[name] = fn(role=role)
        except Exception as exc:  # noqa: BLE001 — see docstring: never kill the loop
            _log.warning("tiers: %s tier failed (%s)", name, exc)
            out[name] = {"ok": False, "error": str(exc)}
    return out


__all__ = ["MESH", "VIEW", "disabled", "distil_mesh", "build_view",
           "run_after_sync",
           "view_nodes", "unrepresented"]
