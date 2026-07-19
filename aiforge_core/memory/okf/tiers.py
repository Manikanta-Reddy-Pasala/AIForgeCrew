"""Two-tier knowledge compaction — the mesh fold, and the local view.

``docs/superpowers/specs/2026-07-20-two-tier-knowledge-compaction.md``.

**Tier 1 — the leader, once per mesh.** Every peer's authored knowledge arrives
by ordinary sync and lands in ``peers/<origin>/``. The elected leader folds that
inbox together with its own ``okf/`` into ``mesh/``: one node per topic/repo
group, each marked ``derived: mesh``. That result is advertised, so it syncs out
to everyone.

**Tier 2 — every peer, locally.** Each machine folds its own ``okf/`` together
with the mesh result into ``view/``, its working view. ``view/`` is regenerated
from scratch, never merged into, and is safe to delete at any moment.

Two rules break the amplification loop, and both are load-bearing:

* ``view/`` is not in ``paths.node_roots()``, so tier-2 output is never
  advertised and can never travel. Were it synced, the leader would fold it into
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
from pathlib import Path

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


# ── bookkeeping ───────────────────────────────────────────────────────────

def _state_path() -> Path:
    from aiforge_core.memory.sync import _io

    return _io.root() / _STATE_FILE


def _read_state() -> dict:
    from aiforge_core.memory.sync import _io

    return _io.read_json(_state_path())


def _save_state(key: str, value: list) -> None:
    from aiforge_core.memory.sync import _io

    state = _read_state()
    state[key] = value
    try:
        _io.write_json(_state_path(), state)
    except OSError as exc:  # a lost stamp costs one redundant fold, never data
        _log.info("tiers: could not record the %s fingerprint (%s)", key, exc)


def _fingerprint(dirs) -> list:
    """Cheap staleness key over ``dirs``: file count, total size, newest mtime.

    The same three facts ``manifest._fingerprint`` uses, and for the same
    reason — it costs a directory walk rather than a read of the tree, and size
    covers the case where two writes land inside one mtime tick.
    """
    from aiforge_core.memory.sync import _io

    count = size = newest = 0
    for directory in dirs:
        for p in _io.iter_syncable(directory, "**/*.md"):
            try:
                st = p.stat()
            except OSError:      # vanished mid-walk; the next pass sees it gone
                continue
            count += 1
            size += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return [count, size, newest]


# ── inputs ────────────────────────────────────────────────────────────────

def _load(dirs) -> list[dict]:
    """Every parsed node under ``dirs``, skipping local-only artefacts.

    ``index.md`` is regenerated navigation and ``.conflict.md`` is a sidecar —
    the manifest excludes both, and feeding either to an LLM would distil
    scaffolding as if it were knowledge.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import _io

    out: list[dict] = []
    for directory in dirs:
        for p in _io.iter_syncable(directory, "**/*.md"):
            if p.name == "index.md" or p.name.endswith(".conflict.md"):
                continue
            try:
                parsed = nodes.parse_node(p.read_text(encoding="utf-8"))
            except OSError:      # unreadable file: skip it, never fail the fold
                continue
            parsed["path"] = p
            out.append(parsed)
    return out


def _derived(node: dict) -> str:
    return str((node.get("meta") or {}).get("derived") or "").strip()


def _authored(nodes_in: list[dict]) -> list[dict]:
    """Tier-1 inputs: only nodes somebody actually authored.

    The anti-amplification filter. Anything already distilled (``derived: mesh``
    in the inbox, because a peer republished it) is dropped here rather than
    re-folded, so mesh knowledge cannot round-trip through the leader and grow.
    """
    return [n for n in nodes_in if not _derived(n)]


def _usable(nodes_in: list[dict]) -> list[dict]:
    """Nodes carrying actual content. An empty or truncated file parses fine and
    yields nothing — folding it would replace knowledge with silence."""
    return [n for n in nodes_in if (n.get("body") or "").strip()]


def _mesh_nodes() -> list[dict]:
    """The tier-1 result as it is visible here.

    A mesh node is identified by its ``derived: mesh`` marker, not by the folder
    it happens to sit in: on the leader it is in ``mesh/``, and on a follower it
    arrives through the ordinary node inbox. One marker, both cases.
    """
    from aiforge_core.memory.sync import paths

    seen: set[Path] = set()
    out: list[dict] = []
    for n in _load((paths.mesh_dir(), paths.peers_root())):
        if _derived(n) != MESH or n["path"] in seen:
            continue
        seen.add(n["path"])
        out.append(n)
    return out


def _mesh_dirs() -> tuple[Path, ...]:
    """Directories a mesh node can appear in — the tier-2 staleness key."""
    from aiforge_core.memory.sync import paths

    return (paths.mesh_dir(), paths.peers_root())


# ── grouping (md_store's, not a second one) ───────────────────────────────

def _facet(node: dict) -> dict:
    """A node seen through md_store's grouping lens.

    ``_group_key`` reads a capture's fields; an OKF node keeps the same facts
    under OKF names. Translating once here is what lets both tiers use the
    grouping ``compact()`` already implements instead of growing a second one.
    """
    from aiforge_core.memory.okf import store

    meta = node.get("meta") or {}
    tags = meta.get("tags")
    return {
        # `scope: repo:<name>` and `workspace:` both mean "this repo" — the
        # store already owns that rule, so it answers rather than a copy here.
        "repo": store._scope_of(str(node.get("type") or ""), meta),
        "topic": meta.get("topic") or "",
        "tags": [str(t) for t in (tags if isinstance(tags, (list, tuple)) else [])],
        "kind": node.get("type") or "note",
    }


def _group_of(node: dict) -> str:
    """This node's topic/repo group. Topic when it has one, else its repo."""
    from aiforge_core.memory.md_store._compact import _NO_TOPIC, _group_key

    facet = _facet(node)
    topic = _group_key(facet, "topic")
    return topic if topic != _NO_TOPIC else _group_key(facet, "repo")


def _grouped(nodes_in: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for n in nodes_in:
        groups.setdefault(_group_of(n), []).append(n)
    return groups


# ── output ────────────────────────────────────────────────────────────────

def _node_id(prefix: str, group: str) -> str:
    """A stable id for a group's node, in the identity alphabet.

    ``paths.sanitise`` rather than a local regex: this id becomes the ``key``
    half of a synced identity, and one that does not round-trip is refused by
    the manifest — silently, which would look like compaction never ran.
    """
    from aiforge_core.memory.sync import paths

    return f"{prefix}-{paths.sanitise(group, 'shared')}"


def _fold(group: str, items: list[dict], role: str) -> dict:
    """Merge one group's nodes into OKR sections.

    ``work_notes.consolidate`` does the merge — it dedupes paraphrases, resolves
    contradictions and maps each item to its section, and degrades to a
    deterministic union+dedupe when no model is reachable. Both tiers regenerate
    from their inputs, so nothing prior is fed back in: a fold that drifts is
    corrected by the next one rather than compounded by it.
    """
    from aiforge_core.runtime import work_notes

    blocks = []
    for n in items:
        title = str((n.get("meta") or {}).get("title") or n.get("id") or "").strip()
        blocks.append((f"### {title}\n\n" if title else "") + (n.get("body") or "").strip())
    return work_notes.consolidate(
        {}, "\n\n".join(b for b in blocks if b), role=role,
        label=f"group '{group}' ({len(items)} node(s))")


def _body(group: str, sections: dict, tags: list[str]) -> str:
    """The rendered OKR body of a compacted node.

    ``work_notes.render_note`` owns section order, scrubbing and link
    normalisation; its frontmatter is dropped because an OKF node carries its
    own (``type``/``id``/``origin``). Rendering the sections by hand here would
    be the same envelope, maintained twice.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.runtime import work_notes

    note = work_notes.render_note(
        "knowledge", group, title=group.replace("-", " ").strip().capitalize(),
        objective=sections.get("objective") or "",
        key_results=sections.get("key_results"), facts=sections.get("facts"),
        links=sections.get("links"), learnings=sections.get("learnings"),
        tags=tags)
    return nodes.parse_node(note)["body"]


def _write(directory: Path, node_id: str, group: str, body: str,
           tags: list[str], derived: str) -> Path | None:
    """Write one compacted node, or skip it when the body is unchanged.

    Skipping matters beyond the write: a rewrite bumps ``rev``, and a mesh node
    whose rev advances every cycle makes every peer re-fetch bytes it already
    has. The prior ``rev`` is carried forward so a real change still wins the
    merge on arrival.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import _io, identity

    path = directory / f"{node_id}.md"
    prior: dict = {}
    if path.is_file():
        try:
            prior = nodes.parse_node(path.read_text(encoding="utf-8"))
        except OSError:
            prior = {}
        if (prior.get("body") or "").strip() == body.strip():
            return None
    meta = {"title": group, "scope": "global", "topic": group, "derived": derived,
            "tags": tags, "rev": (prior.get("meta") or {}).get("rev"),
            "origin": (prior.get("meta") or {}).get("origin")}
    text = nodes.render_node("learning", node_id,
                             identity.stamp({k: v for k, v in meta.items()
                                             if v not in (None, "")}), body)
    try:
        _io.write_atomic(path, text.encode("utf-8"))
    except OSError as exc:  # one unwritable node must not abort the whole fold
        _log.warning("tiers: could not write %s (%s)", path, exc)
        return None
    return path


def _prune(directory: Path, keep: set[str]) -> int:
    """Drop compacted nodes for groups this run no longer produces, so a topic
    that disappeared upstream does not linger as a stale node forever."""
    from aiforge_core.memory.sync import _io

    dropped = 0
    for p in _io.iter_syncable(directory, "*.md"):
        if p.stem in keep:
            continue
        try:
            p.unlink()
            dropped += 1
        except OSError:
            continue
    return dropped


def _run_tier(*, directory: Path, prefix: str, derived: str,
              inputs: list[dict], role: str) -> dict:
    """The half both tiers share: group, fold, render, write, prune."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    keep: set[str] = set()
    for group, items in sorted(_grouped(inputs).items()):
        node_id = _node_id(prefix, group)
        keep.add(node_id)
        tags = sorted({str(t) for n in items
                       for t in ((n.get("meta") or {}).get("tags") or [])})
        path = _write(directory, node_id, group,
                      _body(group, _fold(group, items, role), tags),
                      tags, derived)
        if path is not None:
            written.append(path.name)
    return {"ok": True, "groups": len(keep), "written": written,
            "pruned": _prune(directory, keep)}


# ── tier 1 ────────────────────────────────────────────────────────────────

def distil_mesh(*, role: str = _ROLE) -> dict:
    """Fold every peer's authored knowledge, plus our own, into ``mesh/``.

    Leader-only: the merge is LLM-expensive and non-deterministic, so two peers
    folding the same inbox produce two different answers. ``election.may_distil``
    owns that policy *and* its soft-fail direction (OPEN — a duplicate mesh is
    content-addressed and the next dedupe pass folds it, while losing compaction
    entirely is unrecoverable), so neither is restated here.
    """
    from aiforge_core.memory.sync import election, paths

    if not election.may_distil():
        return {"ok": True, "skipped": "not-leader", "leader": election.leader_name()}

    sources = (paths.okf_dir(), paths.peers_root())
    fingerprint = _fingerprint(sources)
    if _read_state().get("mesh") == fingerprint:
        return {"ok": True, "skipped": "unchanged"}

    inputs = _usable(_authored(_load(sources)))
    if not inputs:
        # Nothing authored anywhere. Returning before the fold also means the
        # prune never runs: a leader that momentarily reads an empty tree must
        # not answer by deleting the mesh everyone else is using.
        _save_state("mesh", fingerprint)
        return {"ok": True, "skipped": "no-inputs", "inputs": 0}
    _log.info("tiers: mesh fold over %d authored node(s)", len(inputs))
    result = _run_tier(directory=paths.mesh_dir(), prefix="M", derived=MESH,
                       inputs=inputs, role=role)
    # Stamped after the fold: a run that dies half way re-reads its inputs next
    # cycle rather than recording work it never finished.
    _save_state("mesh", fingerprint)
    return {**result, "inputs": len(inputs)}


# ── tier 2 ────────────────────────────────────────────────────────────────

def build_view(*, role: str = _ROLE) -> dict:
    """Rebuild ``view/`` from this machine's ``okf/`` plus the mesh result.

    Runs on every peer, the leader included. Skipped unless the mesh actually
    changed, so a cycle where nothing arrived costs no tokens. A mesh that is
    missing, empty or unreadable leaves the previous view exactly where it is:
    a bad mesh must never destroy a good local view.
    """
    from aiforge_core.memory.sync import paths

    fingerprint = _fingerprint(_mesh_dirs())
    if _read_state().get("view") == fingerprint:
        return {"ok": True, "skipped": "unchanged"}

    mesh = _usable(_mesh_nodes())
    if not mesh:
        _log.info("tiers: no usable mesh content — keeping the existing view")
        return {"ok": True, "skipped": "no-mesh"}

    inputs = mesh + _usable(_load((paths.okf_dir(),)))
    _log.info("tiers: view rebuild over %d node(s)", len(inputs))
    result = _run_tier(directory=paths.view_dir(), prefix="V", derived=VIEW,
                       inputs=inputs, role=role)
    _save_state("view", fingerprint)
    return {**result, "inputs": len(inputs)}


# ── the one entry point the sync cycle calls ──────────────────────────────

def run_after_sync(*, role: str = _ROLE) -> dict:
    """Both tiers, once, after a sync pass — so this cycle's arrivals are in.

    Each tier soft-fails independently: compaction is upkeep, and a fold that
    raises must cost a cycle rather than the daemon that would have retried it.
    """
    out: dict = {}
    for name, fn in (("mesh", distil_mesh), ("view", build_view)):
        try:
            out[name] = fn(role=role)
        except Exception as exc:  # noqa: BLE001 — see docstring: never kill the loop
            _log.warning("tiers: %s tier failed (%s)", name, exc)
            out[name] = {"ok": False, "error": str(exc)}
    return out


__all__ = ["MESH", "VIEW", "distil_mesh", "build_view", "run_after_sync"]
