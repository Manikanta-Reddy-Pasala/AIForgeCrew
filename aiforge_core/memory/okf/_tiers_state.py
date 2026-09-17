"""Tier state on disk: the fingerprint of what a tier was built from, and
which facts a tier may use."""
from __future__ import annotations

from pathlib import Path


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.okf.tiers as package
    return package


# Leading/trailing punctuation on a claim line. Stripped only for *comparison*
# in `_unrepresented`, so "port 8080." and "port 8080" count as the same fact —
# part of the "exact-ish whole-line identity" that replaced substring matching.
# Grouped explicitly: `^A|B$` parses as `(^A)|(B$)`, which IS the intended
# strip-both-ends here — but only to a reader who works out the precedence.
def _strip_edge_punct(text: str) -> str:
    r"""Drop leading/trailing non-word characters.

    Two pointers rather than a regex. `^\W+|\W+$` is the shape a scanner flags
    as a denial-of-service risk (and possessive quantifiers only silence the
    engine, not the reviewer): for a strip there is nothing a regex gives that
    a scan does not, and this one is obviously O(n) to anyone reading it.
    """
    i, j = 0, len(text)
    while i < j and not (text[i].isalnum() or text[i] == "_"):
        i += 1
    while j > i and not (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    return text[i:j]


# ── bookkeeping ───────────────────────────────────────────────────────────

def _state_path() -> Path:
    from aiforge_core.memory.sync import _io

    return _io.root() / _pkg()._STATE_FILE


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
        _pkg()._log.info("tiers: could not record the %s fingerprint (%s)", key, exc)


def _fingerprint(dirs) -> list:
    """Cheap staleness key over ``dirs``: file count, total size, newest mtime.

    The same three facts ``manifest._fingerprint`` uses, and for the same
    reason — it costs a directory walk rather than a read of the tree, and size
    covers the case where two writes land inside one mtime tick.
    """
    from aiforge_core.memory.sync import _io

    count = size = newest = 0
    for directory in dirs:
        for p in _io.iter_syncable(directory, _pkg()._MD):
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
        for p in _io.iter_syncable(directory, _pkg()._MD):
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
