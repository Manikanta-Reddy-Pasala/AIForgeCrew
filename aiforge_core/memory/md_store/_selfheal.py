"""Self-healing passes that run INSIDE compaction — no manual cleanup step.

Two defects outlive the write-side guards, because the bad data is already on
disk and the guards only stop new writes:

1. **Global rules that are not global.** ``global`` is injected into every turn
   of every repository as a mandatory rule. On a live install 16 of 36 global
   learnings named a specific file (``calc.py`` seven times) — benchmark
   artifacts that every later turn was told to obey. The write-side fix is in
   :mod:`aiforge_core.memory.scope_guard`; this demotes the ones already
   stored.

2. **Magnet topics.** Slugs like ``code`` / ``data`` / ``build`` match almost
   any query, so facts filed under them surface in unrelated turns. They are
   not empty — on the same install 32 magnet briefs held 326 of 612 facts, 53%
   of all knowledge — so they cannot simply be dropped into ``shared``: that
   trades many bad files for one bad file. Each fact is re-topiced
   individually, through the SAME labeller path a normal compaction uses
   (deterministic snap first, LLM only for what does not snap), a bounded
   number per pass so a single compaction never turns into a migration.

Both passes are bounded, idempotent, and never raise: a self-heal failure must
not cost you the compaction it was riding along with.
"""
from __future__ import annotations

import logging
import os

from ..scope_guard import UNSCOPED, demote_reason
from ._base import _WRITE_LOCK, brief_path, iter_briefs

log = logging.getLogger("aiforge.md_store.selfheal")


def _i_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _enabled(key: str) -> bool:
    return os.environ.get(key, "1") not in ("0", "false", "no")


# ── pass 1: demote globals that name a concrete artifact ─────────────────────

def _global_learnings(store):
    """The stored global learnings, or None when they cannot be read."""
    try:
        return [d for d in store.load_all("global")
                if d.get("type") == "learning"]
    except Exception as exc:  # noqa: BLE001
        log.debug("rescope: load failed: %s", exc)
        return None


def _demote_one(store, d: dict, why: str) -> bool:
    """Rewrite one learning into the unscoped bucket. Content and id are kept;
    only the scope changes.

    A row returned by load_all("global") cannot carry a workspace —
    store._scope_of keys on workspace first, so one would already have been
    filed under its project. There is no repo to restore here.
    """
    meta = dict(d.get("meta") or {})
    meta["scope"] = UNSCOPED
    meta["demoted_from"] = "global"
    meta["demoted_why"] = why
    try:
        r = store.save_node("learning", d.get("id"), meta, d.get("body") or "",
                            reindex=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("rescope: save failed for %s: %s", d.get("id"), exc)
        return False
    return bool(r.get("ok"))


def _demote_batch(store, rows: list, cap: int) -> tuple[int, list]:
    """``(demoted, up to five examples)`` — every learning whose body names a
    file/path/symbol, up to ``cap``."""
    demoted, examples = 0, []
    for d in rows:
        if demoted >= cap:
            break
        why = demote_reason(d.get("body") or "")
        if why and _demote_one(store, d, why):
            demoted += 1
            if len(examples) < 5:
                examples.append(f"{d.get('id')}: {why}")
    return demoted, examples


def rescope_globals(*, limit: int | None = None) -> dict:
    """Demote stored global learnings that name a file/path/symbol.

    They keep their content and id; only the scope changes, so nothing is
    deleted. A demoted learning stops being injected as a mandatory rule for
    every repository — which is the whole point — and moves to an ``unscoped``
    bucket, still on disk and still readable.

    Off with ``AIFORGE_OKR_RESCOPE_GLOBALS=0``.
    """
    if not _enabled("AIFORGE_OKR_RESCOPE_GLOBALS"):
        return {"demoted": 0, "skipped": "disabled"}
    try:
        from ..okf import store
    except Exception:  # noqa: BLE001
        return {"demoted": 0, "skipped": "okf unavailable"}
    rows = _global_learnings(store)
    if rows is None:
        return {"demoted": 0, "error": "load failed"}
    cap = limit if limit is not None else _i_env("AIFORGE_OKR_RESCOPE_MAX", 50)

    demoted, examples = _demote_batch(store, rows, cap)
    if demoted:
        try:
            store._write_index()
        except Exception:  # noqa: BLE001
            pass
        log.info("rescope_globals: demoted %d global learning(s)", demoted)
    return {"demoted": demoted, "examples": examples}


# ── pass 2: re-topic the facts trapped in magnet briefs ──────────────────────

def _magnet_briefs() -> list[str]:
    """Existing topic briefs whose NAME is a recall magnet or junk."""
    from . import _topics
    skip = _topics._repo_brief_names() | {"shared"}
    out = []
    for p in iter_briefs():
        key = p.stem[len("compacted-"):]
        if key in skip or _topics.topic_ok(key):
            continue
        out.append(key)
    return sorted(out)


def _facts_of(key: str) -> list[str]:
    from aiforge_core.runtime import work_notes
    try:
        return list(work_notes.parse_note(
            brief_path(key).read_text(encoding="utf-8"))["sections"].get("facts") or [])
    except Exception:  # noqa: BLE001
        return []


def _magnet_batch(magnets: list, cap: int) -> tuple[list, dict]:
    """``(batch, origin)`` — a bounded batch of facts, remembering which brief
    each came from."""
    from ._render import _fact_body
    batch: list[dict] = []
    origin: dict[str, str] = {}
    for key in magnets:
        for i, fact in enumerate(_facts_of(key)):
            if len(batch) >= cap:
                return batch, origin
            body = _fact_body(fact)
            fid = f"{key}#{i}"
            batch.append({"file": fid, "title": body[:120], "body": body,
                          "_fact": fact})
            origin[fid] = key
    return batch, origin


def _plan_moves(batch: list, labels: dict, origin: dict) -> dict:
    """``{source brief: [(item, target)]}``.

    Planned BEFORE any write: ``_brief_upsert`` takes _WRITE_LOCK itself and
    that lock is NOT reentrant, so calling it from inside the lock
    self-deadlocks. Plan unlocked, upsert unlocked (each call locks for its own
    write), and take the lock only for the source rewrite.
    """
    by_src: dict[str, list[tuple[dict, str]]] = {}
    for item in batch:
        target = labels.get(item["file"])
        src = origin[item["file"]]
        if target and target != src:   # else unplaceable, or already home
            by_src.setdefault(src, []).append((item, target))
    return by_src


def _copy_to_targets(items: list) -> list:
    """Write each fact to its new brief; returns the ones that landed. A fact
    that fails to copy stays in the magnet and is retried next pass."""
    from ._render import _brief_upsert
    landed: list[dict] = []
    for item, target in items:
        try:
            _brief_upsert(target, item["body"], topic=target)
        except Exception as exc:  # noqa: BLE001
            log.debug("relabel: upsert %s failed: %s", target, exc)
            continue
        landed.append(item)
    return landed


def _drop_from_source(src: str, landed: list) -> int:
    """Remove the copied facts from the magnet brief. Re-reads under the lock:
    a concurrent write may have changed the brief since we planned, and
    dropping facts we never read would lose them."""
    from aiforge_core.runtime import work_notes
    with _WRITE_LOCK:
        keep = _facts_of(src)
        for item in landed:
            if item["_fact"] in keep:
                keep.remove(item["_fact"])
        try:
            work_notes.update_note(str(brief_path(src)), facts=keep,
                                   kind="knowledge", key=src)
            return len(landed)
        except Exception as exc:  # noqa: BLE001 — the facts are already copied
            log.debug("relabel: rewrite %s failed: %s", src, exc)
            return 0


def relabel_magnet_facts(*, limit: int | None = None,
                         model_role: str = "learner") -> dict:
    """Move facts out of magnet topics onto real subjects, a bounded batch at a
    time.

    Each fact is routed by the SAME machinery a normal compaction uses —
    ``_topic_labels``, which snaps deterministically against the existing
    vocabulary first and only asks the model about what does not snap. A fact
    the labeller cannot place is left where it is rather than guessed at; it
    will be offered again next pass, when the vocabulary may have grown.

    A magnet brief emptied by this pass is archived by the existing
    empty-brief sweep. Off with ``AIFORGE_OKR_RELABEL_MAGNETS=0``; batch size
    ``AIFORGE_OKR_RELABEL_MAX`` (default 25).
    """
    if not _enabled("AIFORGE_OKR_RELABEL_MAGNETS"):
        return {"moved": 0, "skipped": "disabled"}
    magnets = _magnet_briefs()
    if not magnets:
        return {"moved": 0, "magnets": 0}
    cap = limit if limit is not None else _i_env("AIFORGE_OKR_RELABEL_MAX", 25)
    batch, origin = _magnet_batch(magnets, cap)
    if not batch:
        return {"moved": 0, "magnets": len(magnets)}

    from ._compact import _topic_labels
    try:
        labels = _topic_labels(batch, model_role)
    except Exception as exc:  # noqa: BLE001
        log.debug("relabel: labeller failed: %s", exc)
        return {"moved": 0, "magnets": len(magnets), "error": str(exc)}

    moved = 0
    for src, items in _plan_moves(batch, labels, origin).items():
        landed = _copy_to_targets(items)
        if landed:
            moved += _drop_from_source(src, landed)
    if moved:
        log.info("relabel_magnet_facts: moved %d fact(s) out of %d magnet(s)",
                 moved, len(magnets))
    return {"moved": moved, "magnets": len(magnets), "batch": len(batch)}


def run_all(*, model_role: str = "learner", summarize: bool = False) -> dict:
    """Every self-heal pass, bounded. Called from ``compact()`` so auto-compact
    and compact-all both repair as they go. Never raises.

    ``rescope_globals`` is pure and always runs. ``relabel_magnet_facts`` needs
    to place each fact on a subject, which without a real embedding backend
    means an LLM call — so it runs ONLY when the compaction was already going
    to summarise. A ``summarize=False`` pass is the deterministic, offline
    mode; making it wait on a model (or on a model's retry timeout) would
    change what that mode means.
    """
    out: dict = {"rescope_globals": {}, "relabel_magnets": {}}
    try:
        out["rescope_globals"] = rescope_globals()
    except Exception as exc:  # noqa: BLE001
        log.debug("selfheal rescope_globals failed: %s", exc)
        out["rescope_globals"] = {"error": str(exc)}
    if not summarize:
        out["relabel_magnets"] = {"moved": 0, "skipped": "deterministic pass"}
        return out
    try:
        out["relabel_magnets"] = relabel_magnet_facts(model_role=model_role)
    except Exception as exc:  # noqa: BLE001
        log.debug("selfheal relabel_magnets failed: %s", exc)
        out["relabel_magnets"] = {"error": str(exc)}
    return out


__all__ = ["rescope_globals", "relabel_magnet_facts", "run_all"]
