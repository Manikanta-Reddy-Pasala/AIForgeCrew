"""Two-tier knowledge compaction — the mesh fold, and the local view.

``docs/superpowers/specs/2026-07-20-two-tier-knowledge-compaction.md``.

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

import hashlib
import logging
import re
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

# How much of a group name survives into its node id. The digest beside it, not
# the slug, is what keeps two groups apart, so this only has to stay readable —
# and short enough that the id clears ``paths.is_addressable``'s length cap.
_ID_SLUG_MAX = 48

# A rendered list marker at the start of a line. Bodies are re-folded, and OKR
# rendering already put these there: feeding them back in produced "- - fact".
_BULLET_RE = re.compile(r"^\s*[-*]\s+")

# Leading/trailing punctuation on a claim line. Stripped only for *comparison*
# in `_unrepresented`, so "port 8080." and "port 8080" count as the same fact —
# part of the "exact-ish whole-line identity" that replaced substring matching.
# Grouped explicitly: `^A|B$` parses as `(^A)|(B$)`, which IS the intended
# strip-both-ends here — but only to a reader who works out the precedence.
# POSSESSIVE quantifiers (`++`, Python 3.11+ and this project requires >=3.11).
# `\W+$`-style strips backtrack super-linearly on input that does NOT match:
# the engine retries the run at every length before giving up. `++` never
# gives characters back, which is exactly right for a strip and turns the
# scan linear.
_EDGE_PUNCT_RE = re.compile(r"(?:^\W++)|(?:\W++$)")


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


def _combine_fp(a: list, b: list) -> list:
    """Merge two fingerprints as if taken over one set of dirs.

    ``_fingerprint`` sums file counts and sizes and maxes the newest mtime across
    its dirs, and ``okf/``, ``peers/`` and ``mesh/`` are disjoint — so combining a
    fingerprint of the inputs with one of ``mesh/`` reproduces
    ``_fingerprint(_tier1_dirs())`` exactly, but lets the two halves be sampled at
    different instants. That is what tier 1 needs (see :func:`distil_mesh`): the
    inputs frozen before the fold, ``mesh/`` read after it.
    """
    return [a[0] + b[0], a[1] + b[1], max(a[2], b[2])]


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
    re-folded, so mesh knowledge cannot round-trip through the admin and grow.
    """
    return [n for n in nodes_in if not _derived(n)]


def _usable(nodes_in: list[dict]) -> list[dict]:
    """Nodes carrying actual content. An empty or truncated file parses fine and
    yields nothing — folding it would replace knowledge with silence."""
    return [n for n in nodes_in if (n.get("body") or "").strip()]


def _origin(node: dict) -> str:
    """This node's minting peer, in the form ids are compared in."""
    from aiforge_core.memory.sync import paths

    return paths.fold(str((node.get("meta") or {}).get("origin") or ""))


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
        _log.info("tiers: cannot name the admin (%s) — trusting only our own fold", exc)
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
            if _derived(n) != MESH or _origin(n) != admin or n["path"] in seen:
                continue
            owner = n["path"].relative_to(root).parts[:-1]
            if owner and paths.fold(owner[0]) != admin:
                _log.warning("tiers: mesh node %s claims the admin's origin but "
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
    return "\n".join(_BULLET_RE.sub("", ln) for ln in body.splitlines())


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
        norm = _EDGE_PUNCT_RE.sub("", line.lower())
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
    return f"{prefix}-{paths.sanitise(group, 'shared')[:_ID_SLUG_MAX]}-{digest}"


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
    for p in _io.iter_syncable(directory, "**/*.md"):
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
    """Fold every peer's authored knowledge, plus our own, into ``mesh/``.

    Admin-only — the one step that is: the merge is LLM-expensive and
    non-deterministic, so two machines folding the same inbox produce two
    different answers. ``role.may_merge`` owns that policy *and* its soft-fail
    direction (OPEN — a machine with no admin configured IS the admin and must
    keep merging), so neither is restated here. Everything else about compaction
    stays local: see :func:`build_view` and ``md_store.compact``.
    """
    from aiforge_core.memory.sync import paths, role as _role

    if not _role.may_merge():
        return {"ok": True, "skipped": "not-admin", "admin": _role.admin_id()}

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
    result = _run_tier(directory=paths.view_dir(), prefix="V", derived=VIEW,
                       inputs=inputs, role=role)
    _save_state("view", fingerprint)
    return {**result, "inputs": len(inputs)}


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
    from aiforge_core.memory.sync import identity, merge, paths, role as _role
    from aiforge_core.memory.sync import tombstone

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


__all__ = ["MESH", "VIEW", "distil_mesh", "build_view", "run_after_sync",
           "view_nodes", "unrepresented"]
