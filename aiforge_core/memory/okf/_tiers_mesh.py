"""Building a mesh tier: which nodes, grouped how, and how they are written
and pruned."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ._tiers_state import (
    _derived,
    _load,
    _origin,
    _strip_edge_punct,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.okf.tiers as package
    return package


def _trusted_origin() -> str:
    """The one machine whose ``derived: mesh`` nodes this one will fold.

    Trust-by-configuration, NOT cryptography: ``derived: mesh`` is ordinary
    frontmatter, so anything that can push could stamp it on a node. Unchecked,
    that node landed in ``mesh/`` and was folded straight into ``view/`` — the
    working knowledge agents read — i.e. an LLM-instruction injection channel
    with hub-wide reach. Signed manifests are the real fix and are out of scope
    here; the admin additionally refuses a pushed node carrying the marker at
    all (``sync.inbox.accept``), so the two halves meet in the middle.

    That machine is the admin: ``role.admin_id()`` on a spoke (learned from the
    admin's manifest response), and our own id on the admin itself.

    Falls back to our own id when the admin is not known yet — a view built from
    our own fold alone is a far smaller loss than one built from whatever
    somebody else asked us to believe.
    """
    from aiforge_core.memory.sync import identity, paths, role

    try:
        return paths.fold(role.admin_id()) or paths.fold(identity.self_id())
    except Exception as exc:  # noqa: BLE001 — unreadable config must not widen trust
        _pkg()._log.info("tiers: cannot name the admin (%s) — trusting only our own fold", exc)
        return paths.fold(identity.self_id())


def _mesh_nodes() -> list[dict]:
    """The tier-1 result as it is visible here.

    A mesh node is identified by its ``derived: mesh`` marker plus an ``origin``
    naming the admin, not by the folder it sits in.
    ``paths.target_for`` routes an arriving mesh node to ``mesh/``, so that is
    where it normally lives on a follower too — but the inbox is still read,
    because a build from before that routing (or a node received before it) left
    its copy in ``peers/``. One marker, either folder — and a node from anyone
    but the admin is left to be treated as an ordinary foreign node, which
    ``_authored`` then discards from the fold.

    Both folders key on the minting peer (``mesh/<origin>/`` and
    ``peers/<origin>/``), so a node received from the network carries a *second*
    statement of who minted it — and that one is written by ``apply``, which
    only accepts a node whose ``origin`` is the peer that served it. Requiring
    the two to agree is defence in depth for what is already on disk: a node
    planted before that check existed — ``peers/nuc/M-99.md`` whose frontmatter
    claims ``origin: <admin>`` — would otherwise still be folded into
    ``view/``, the only thing retrieval surfaces to agents.

    A node sitting directly in ``mesh/`` or ``peers/`` carries no such second
    statement: nothing arriving over the network can land there (every write
    target is ``<root>/<origin>/<key>.md``), so it is a local artefact of this
    machine — a fold from a build before the per-origin split, or an operator's
    own file — and is judged on its frontmatter alone as before.
    """
    from aiforge_core.memory.sync import paths

    admin = _trusted_origin()
    seen: set[Path] = set()
    out: list[dict] = []
    for root in (paths.mesh_dir(), paths.peers_root()):
        for n in _load((root,)):
            if _derived(n) != _pkg().MESH or _origin(n) != admin or n["path"] in seen:
                continue
            owner = n["path"].relative_to(root).parts[:-1]
            if owner and paths.fold(owner[0]) != admin:
                _pkg()._log.warning("tiers: mesh node %s claims the admin's origin but "
                             "was filed under %s — not folding it into the view",
                             n["path"].name, owner[0])
                continue
            seen.add(n["path"])
            out.append(n)
    return out


def _unbulleted(body: str) -> str:
    """``body`` with rendered list markers stripped from the start of each line.

    Both tiers fold already-rendered OKR markdown, and ``render_note`` puts the
    markers back: without this the re-fold read ``- fact`` as the fact itself
    and rendered ``- - fact``, one marker deeper on every round.
    """
    return "\n".join(_pkg()._BULLET_RE.sub("", ln) for ln in body.splitlines())


def _claims(node: dict) -> list[str]:
    """A node's body as comparable content lines: markers and headings dropped,
    each surviving line normalised for case, whitespace and surrounding
    punctuation. This normalised whole line is the unit ``_unrepresented``
    compares — never a substring of it."""
    out: list[str] = []
    for raw in _unbulleted(node.get("body") or "").splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        norm = _strip_edge_punct(line.lower())
        if norm:
            out.append(norm)
    return out


def _unrepresented(local: list[dict], mesh: list[dict]) -> list[dict]:
    """Local nodes the mesh does not already carry.

    Tier 1 folded this machine's ``okf/`` into the mesh, so handing tier 2 both
    merges the same knowledge twice — every fact rendered twice in the view, and
    with no model reachable the deterministic merge has nothing to dedupe it
    away. Bounded at 2x rather than amplifying, but still wrong.

    Representation is WHOLE-LINE identity, never substring containment. The old
    ``claim not in "\\n".join(...)`` test declared a claim represented whenever it
    appeared *inside* any mesh line, so a local "use port 8080" was suppressed by
    the mesh's "never use port 8080 for the gateway" — the negation swallowed its
    own affirmation, and since ``unrepresented`` also gates recall the agent was
    served only the negation. It also dropped any node whose body was
    headings-only (no claims → ``any()`` over nothing → ``False``). Both are
    fixed here: a claim counts as carried only when a whole mesh line equals it,
    and a node that states nothing comparable is kept rather than silently lost.
    """
    carried = {c for n in mesh for c in _claims(n)}
    kept: list[dict] = []
    for n in local:
        claims = _claims(n)
        if not claims or any(c not in carried for c in claims):
            kept.append(n)
    return kept


def _mesh_dirs() -> tuple[Path, ...]:
    """Directories a mesh node can appear in."""
    from aiforge_core.memory.sync import paths

    return (paths.mesh_dir(), paths.peers_root())


def _own_mesh_dir() -> Path:
    """Where *our* fold is written: this peer's own subtree of ``mesh/``.

    Derived from ``paths.mesh_node_path`` — that function owns the
    ``mesh/<origin>/<key>.md`` shape, and the admin must write exactly where a
    spoke will file the same node — rather than spelling the layout again
    here. Owning a whole subtree is what makes the prune safe: everything under
    it is ours to delete, and another machine's fold is not.
    """
    from aiforge_core.memory.sync import identity, paths

    return paths.mesh_node_path(identity.self_id(), "key").parent


def _tier1_dirs() -> tuple[Path, ...]:
    """Tier 1's staleness key: its inputs *and* its output.

    ``mesh/`` is in here because the fold is the only thing that repairs it. Key
    on the inputs alone and a mesh destroyed from outside — a hand-deleted
    directory, a tombstone for what its frontmatter called its node — stays
    destroyed everywhere until somebody happens to author a new note.
    """
    from aiforge_core.memory.sync import paths

    return (paths.okf_dir(), paths.peers_root(), paths.mesh_dir())


def _view_dirs() -> tuple[Path, ...]:
    """Everything tier 2 reads — its staleness key.

    ``okf/`` is in here because it is an input: keying on the mesh alone meant a
    note authored locally stayed out of the local view until the fold ran
    again — a full cycle away.
    """
    from aiforge_core.memory.sync import paths

    return (paths.okf_dir(), *_mesh_dirs())


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

    The digest is what keeps distinct groups apart. Sanitisation is lossy —
    ``pos repo``, ``pos-repo`` and ``pos/repo`` all reduce to ``pos-repo`` — so
    the slug alone made three folds overwrite one file and two thirds of the
    knowledge vanished inside a single run, while ``rev`` was bumped once per
    collision and every peer re-fetched the survivor. Hashing the raw group
    string also makes the truncation above safe.
    """
    from aiforge_core.memory.sync import paths

    digest = hashlib.sha256(group.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{paths.sanitise(group, 'shared')[:_pkg()._ID_SLUG_MAX]}-{digest}"


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
        blocks.append((f"### {title}\n\n" if title else "")
                      + _unbulleted(n.get("body") or "").strip())
    # The LLM distillation rides the same evening window as md_store's folds, so
    # the "compact group" calls stop crowding the working day. Outside the
    # window (or when compaction is off) consolidate degrades to a deterministic
    # union+dedupe — the node still updates for the mesh; only the LLM refine
    # waits. open_now() returns True whenever no daily pass is registered, so a
    # machine without an evening schedule still folds anytime (no starvation).
    from aiforge_core.runtime import compact_window as _cw
    return work_notes.consolidate(
        {}, "\n\n".join(b for b in blocks if b), role=role,
        label=f"group '{group}' ({len(items)} node(s))",
        allow_llm=_cw.open_now())


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
        _pkg()._log.warning("tiers: could not write %s (%s)", path, exc)
        return None
    return path


def _prune(directory: Path, keep: set[str]) -> int:
    """Drop compacted nodes for groups this run no longer produces, so a topic
    that disappeared upstream does not linger as a stale node forever.

    ``directory`` is always a tree this peer owns outright — its own subtree of
    ``mesh/``, or ``view/`` — because a delete here leaves no tombstone: pruning
    a *foreign* mesh node deleted a file the next pull simply fetched again, one
    wasted transfer and delete per cycle forever, with the two peers permanently
    disagreeing about the view. Removing a node mesh-wide is
    ``tombstone.delete_node(origin, key)``, which propagates.

    Recursive, so it still sees its nodes if a fold ever nests them; ``*.md``
    stopped matching anything once the mesh gained its ``<origin>/`` level.
    """
    from aiforge_core.memory.sync import _io

    dropped = 0
    for p in _io.iter_syncable(directory, _pkg()._MD):
        if p.stem in keep:
            continue
        try:
            p.unlink()
            dropped += 1
        except OSError:
            continue
    return dropped
