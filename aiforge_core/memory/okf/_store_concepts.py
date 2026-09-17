"""Finding nodes by concept, and removing duplicate nodes."""
from __future__ import annotations

import os
import re


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.okf.store as package
    return package


def _norm_concept(s: str) -> str:
    """Normalize concept text for identity comparison — lowercase, keep only
    alphanumerics + spaces, collapse whitespace. Shared by the write-time
    concept lookup and the post-hoc dedupe so both agree on 'same concept'."""
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9 ]", "", (s or "").lower())).strip()


def _concept_of(d: dict) -> str:
    """The concept text of a parsed node — body first (learnings/facts share the
    same rule text even when titles differ), else description, else title."""
    m = d.get("meta") or {}
    return _norm_concept(d.get("body") or m.get("description") or m.get("title") or "")


def _concept_threshold(threshold: float | None) -> float:
    """The similarity cutoff — the caller's, else the env default, else 0.86."""
    if threshold is not None:
        return threshold
    try:
        return float(os.environ.get("AIFORGE_OKF_CONCEPT_SIMILARITY", "0.86"))
    except (TypeError, ValueError):
        return 0.86


def _best_concept_match(node_type: str, want_scope: str, target: str,
                        threshold: float) -> str | None:
    """Scan same-type, same-scope nodes for a concept match. An EXACT normalized
    hit wins immediately; otherwise the closest fuzzy match at or above
    ``threshold``. None when nothing qualifies."""
    import difflib
    best: tuple[str, float] | None = None
    for d in _pkg().load_all():
        if d.get("type") != node_type:
            continue
        if _pkg()._scope_of(node_type, d.get("meta") or {}) != want_scope:
            continue
        cand = _concept_of(d)
        if not cand:
            continue
        if cand == target:
            return d.get("id")
        r = difflib.SequenceMatcher(None, target, cand).ratio()
        if r >= threshold and (best is None or r > best[1]):
            best = (d.get("id"), r)
    return best[0] if best else None


def find_by_concept(node_type: str, meta: dict, concept_text: str,
                    *, threshold: float | None = None) -> str | None:
    """Return the id of an EXISTING node of the same ``node_type`` and the same
    resolved SCOPE whose concept text matches ``concept_text`` — exactly, or
    fuzzily above ``threshold``. Lets a writer REUSE the concept's file (pass the
    id back to :func:`save_node`) instead of minting a new incrementing id, so
    types with no natural title key (learnings, key_results) still honour OKF
    'one concept = one file' (≤1 per scope → ≤2 total: global + project).
    ``None`` when no match. Soft-fail (returns None on any error)."""
    target = _norm_concept(concept_text)
    if not target:
        return None
    try:
        return _best_concept_match(node_type, _pkg()._scope_of(node_type, meta or {}),
                                   target, _concept_threshold(threshold))
    except Exception:  # noqa: BLE001 — lookup must never break a write
        return None


def _is_concept_dup(key_text: str, prior: list[str], threshold: float) -> bool:
    """True when ``key_text`` matches an already-kept concept — exactly, or
    fuzzily at/above ``threshold``."""
    import difflib
    return key_text in prior or any(
        difflib.SequenceMatcher(None, key_text, p).ratio() >= threshold
        for p in prior)


def _tombstone_loser(d: dict, _tomb) -> None:
    """Mark a deleted duplicate deleted TO THE MESH — an unlink alone is undone
    by the next pull from any peer still holding it. Only the loser: the
    survivor keeps its identity, and ``mark_deleted`` refuses anything this
    machine did not mint."""
    m = d.get("meta") or {}
    _tomb.mark_deleted(m.get("origin"), d.get("id"), m.get("rev"))


def dedupe_nodes() -> dict:
    """Remove DUPLICATE OKR nodes — same type + same SCOPE + same-or-NEAR
    content. Keeps the first (lowest id), deletes the rest. Matches EXACTLY on
    normalized content and FUZZILY (difflib >= AIFORGE_OKF_CONCEPT_SIMILARITY,
    default 0.86) so paraphrases of one concept — the L-01/L-07/L-13 pile-up the
    learner produced over repeated runs — collapse to a single file, restoring
    OKF 'one concept = one file'. Returns {ok, removed, kept}. Soft-fail.

    Local work on local files, on every machine: it only ever collapses nodes
    this machine minted — ``tombstone.mark_deleted`` refuses another origin — so
    there is nothing here for the admin to arbitrate. The cross-machine merge is
    the separate, admin-only step (``okf.tiers.distil_mesh``)."""
    from aiforge_core.memory.sync import tombstone as _tomb  # lazy: heavy package

    threshold = _concept_threshold(None)
    # bucket kept concepts by (type, scope) so fuzzy compares stay in-scope
    kept: dict[tuple, list[str]] = {}
    removed = 0
    try:
        nodes = sorted(_pkg().load_all(), key=lambda d: str(d.get("id") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    for d in nodes:
        key_text = _concept_of(d)
        if not key_text:
            continue
        prior = kept.setdefault(
            (d.get("type"), _pkg()._scope_label_from_path(d.get("path", ""))), [])
        if not _is_concept_dup(key_text, prior, threshold):
            prior.append(key_text)
            continue
        try:
            os.unlink(d["path"])
            removed += 1
        except OSError:
            continue
        _tombstone_loser(d, _tomb)
    if removed:
        _pkg()._invalidate()
        _pkg()._write_index()
    return {"ok": True, "removed": removed,
            "kept": sum(len(v) for v in kept.values())}

