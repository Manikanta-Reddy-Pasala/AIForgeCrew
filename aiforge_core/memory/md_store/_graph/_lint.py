"""Graph layer — deterministic link-health lint plus scope self-heal / reheal
recovery. Finds broken/orphan brief links and repairs them, and moves
mis-scoped facts to the global brief (with a recovery pass). Part of the
``_graph`` package (split from the former flat ``_graph``)."""
from __future__ import annotations

import os

from .._base import (
    _CAPTURE_SIG_RE,
    _COMPACT_LOCK,
    _WRITE_LOCK,
    _capture_md_files,
    _log,
    _parse,
    brief_path,
    iter_briefs,
)
from .._capture import capture
from .._render import _fact_body
from .._scope import classify_scope, classify_scopes
from ._map import _live_briefs


def lint_graph(*, repair: bool = False) -> dict:
    """Deterministic graph-health lint over the brief link structure (the video's
    periodic linting step). Finds:

    * ``broken`` — a brief's Links entry pointing at a ``compacted-*.md`` file
      that no longer exists (a dangling ref after a brief was deleted/renamed).
    * ``orphans`` — briefs with NO brief-to-brief link either way (candidates for
      the next ``map_scopes`` pass; reported, never deleted — an orphan may be a
      genuinely standalone topic).

    With ``repair=True`` the broken refs are STRIPPED from each brief's Links
    (real URLs / jira refs are kept). Never raises. Returns
    ``{broken, orphans, repaired}``."""
    from aiforge_core.runtime import work_notes
    briefs = [p for p in iter_briefs() if not _CAPTURE_SIG_RE.search(p.name)]
    existing = {p.name for p in briefs}
    linked: set[str] = set()          # keys that have ANY brief-ref (in or out)
    broken: list[dict] = []
    repaired = 0
    parsed_cache: dict = {}
    for p in briefs:
        key = p.stem[len("compacted-"):]
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        parsed_cache[p] = parsed
        for lk in (parsed["sections"].get("links") or []):
            m = work_notes._BRIEF_REF_RE.match(lk.strip())
            if not m:
                continue
            tgt = m.group("file")                     # compacted-<x>.md
            if tgt in existing:
                linked.add(key)
                linked.add(tgt[len("compacted-"):-len(".md")])
            else:
                broken.append({"brief": key, "ref": tgt})
    if repair and broken:
        with _WRITE_LOCK:
            for p in briefs:
                parsed = parsed_cache.get(p)
                if not parsed:
                    continue
                links = list(parsed["sections"].get("links") or [])
                kept = [l for l in links
                        if not (lambda mm: mm and mm.group("file") not in existing)(
                            work_notes._BRIEF_REF_RE.match(l.strip()))]
                if len(kept) != len(links):
                    repaired += len(links) - len(kept)
                    work_notes.update_note(str(p), links=kept, kind="knowledge",
                                           key=p.stem[len("compacted-"):])
    orphans = sorted(p.stem[len("compacted-"):] for p in briefs
                     if p.stem[len("compacted-"):] not in linked)
    return {"broken": broken, "orphans": orphans, "repaired": repaired}


def _remove_facts_locked(path, key: str, remove_ci_keys: set) -> int:
    """Remove facts whose ``_ci_key(_fact_body(f))`` is in ``remove_ci_keys`` from
    a brief, re-reading it FRESH under ``_WRITE_LOCK`` so a concurrent capture
    (which also holds ``_WRITE_LOCK``) is never clobbered by a stale snapshot.
    Returns the count removed. Never raises on a bad path."""
    from aiforge_core.runtime import work_notes
    with _WRITE_LOCK:
        try:
            parsed = work_notes.parse_note(path.read_text(encoding="utf-8"))
        except OSError:
            return 0
        facts = parsed["sections"].get("facts") or []
        kept = [f for f in facts
                if work_notes._ci_key(_fact_body(f)) not in remove_ci_keys]
        removed = len(facts) - len(kept)
        if removed:
            work_notes.update_note(str(path), facts=kept, kind="knowledge",
                                   key=key)
        return removed


def reheal_scopes(*, role: str = "learner", max_per_brief: int = 60) -> dict:
    """Self-heal mis-scoped facts: re-classify each fact in every PROJECT/TOPIC
    brief and MOVE the ones that are actually global into the shared brief
    (facts captured before scope classification, or mis-hinted, end up in the
    wrong brief). The shared/global brief is never demoted into a project. Gated
    on ``AIFORGE_OKR_SCOPE_LLM`` (off → no-op). Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"moved": 0, "skipped": "llm_off"}
    from aiforge_core.runtime import work_notes
    moved = 0
    healed: list[str] = []
    # _COMPACT_LOCK (NOT _WRITE_LOCK): capture()→_brief_upsert takes _WRITE_LOCK,
    # so holding it here would self-deadlock. This serialises reheal against
    # compaction, which is the right granularity.
    with _COMPACT_LOCK:
        for b in _live_briefs():
            key = b["key"]
            if key == "shared":            # global brief — nothing to promote out
                continue
            try:
                parsed = work_notes.parse_note(b["path"].read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            if not facts:
                continue
            moved_facts: list[str] = []
            batch = facts[:max_per_brief]
            try:                       # ONE call for the brief, not one per fact
                verdicts = classify_scopes(batch, hint_repo=key, role=role)
            except Exception:  # noqa: BLE001
                continue
            for f, sc in zip(batch, verdicts):
                if sc.get("fallback") or sc["scope"] != "global":
                    continue
                try:                              # → shared brief via capture
                    capture("learning", f, repo=None, classify=False,
                            source="reheal")
                except Exception:  # noqa: BLE001
                    continue                      # couldn't move → leave in place
                moved += 1
                moved_facts.append(f)
                # W4: drop the stale project-scoped INDEX row so the moved fact
                # isn't duplicated under both the old repo and 'shared'.
                try:
                    from aiforge_core.memory import backend_select, sqlite_memory
                    if backend_select.embedded():
                        # exclude 'compacted' so we delete the stale per-capture
                        # row, NOT the brief row (whose text contains every fact).
                        sqlite_memory.delete_by_text_contains(
                            _fact_body(f), repo=key, exclude_kind="knowledge")
                except Exception:  # noqa: BLE001
                    pass
            if not moved_facts:
                continue
            # Remove ONLY the moved facts, re-reading the brief FRESH under
            # _WRITE_LOCK — a concurrent capture that landed during the (slow)
            # classification is preserved instead of clobbered by a stale snapshot.
            _remove_facts_locked(b["path"], key, {
                work_notes._ci_key(_fact_body(x)) for x in moved_facts})
            healed.append(key)
    return {"moved": moved, "healed": healed}


def cleanup_reheal(*, role: str = "learner") -> dict:
    """Recovery for an over-aggressive reheal: re-classify each moved
    (``source: reheal``) fact ON ITS OWN and DELETE the ones that are not truly
    global — strip them from ``compacted-shared.md`` and remove their capture
    files. Origin repo was not recorded, so a non-global fact is removed, not
    restored to its project. Run BEFORE any recompaction reword the shared facts
    (matching is verbatim). Gated on ``AIFORGE_OKR_SCOPE_LLM``. Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"checked": 0, "removed": 0, "skipped": "llm_off"}
    from aiforge_core.runtime import work_notes
    reheal: list[dict] = []
    for p in _capture_md_files():
        if p.name.startswith("compacted-"):
            continue
        try:
            d = _parse(p)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("source") or "") != "reheal":
            continue
        head = (d.get("body") or "").strip().splitlines()
        fact = head[0].strip().lstrip("-* ").strip() if head else ""
        if fact:
            reheal.append({"path": p, "fact": fact})
    if not reheal:
        return {"checked": 0, "removed": 0}

    remove_keys: set[str] = set()
    remove_paths: list = []
    checked = 0
    try:      # judged on their own merit, but in ONE call per batch of facts
        verdicts = classify_scopes([r["fact"] for r in reheal], role=role)
    except Exception:  # noqa: BLE001
        verdicts = []
    # zip() would silently truncate — and this loop DELETES, so a short list
    # must skip the tail explicitly, never shorten the work without saying so.
    if len(verdicts) != len(reheal):
        _log.warning("cleanup_reheal: %d verdicts for %d facts — skipping the rest",
                     len(verdicts), len(reheal))
    for r, sc in zip(reheal, verdicts):
        checked += 1
        if sc.get("fallback"):
            # No model verdict (batch failed / no entry). "Not global" is then
            # absence of evidence, and acting on it here DELETES the fact from
            # the shared brief and unlinks its capture file.
            continue
        if sc["scope"] != "global":
            remove_keys.add(work_notes._ci_key(_fact_body(r["fact"])))
            remove_paths.append(r["path"])

    removed = 0
    matched: set[str] = set()
    if remove_keys:
        # Fresh read-modify-write under _WRITE_LOCK (concurrent captures to shared
        # also take _WRITE_LOCK — don't clobber them with a stale snapshot).
        with _WRITE_LOCK:
            shared = brief_path("shared")
            if shared.is_file():
                parsed = work_notes.parse_note(shared.read_text(encoding="utf-8"))
                facts = parsed["sections"].get("facts") or []
                kept = []
                for f in facts:
                    k = work_notes._ci_key(_fact_body(f))
                    if k in remove_keys:
                        matched.add(k)
                        removed += 1
                    else:
                        kept.append(f)
                if removed:
                    work_notes.update_note(str(shared), facts=kept,
                                           kind="knowledge", key="shared")
    # P2 shortfall guard: only delete a capture file whose fact was actually
    # found + removed from shared. A flagged fact NOT found (the shared brief was
    # reworded by a compaction since reheal) keeps its capture file, so the
    # recovery source isn't lost while an orphan remains in the brief.
    deleted = 0
    orphaned = 0
    for r in reheal:
        _rk = work_notes._ci_key(_fact_body(r["fact"]))
        if _rk not in remove_keys:
            continue                              # stays global — keep
        if _rk in matched:
            try:
                r["path"].unlink()
                deleted += 1
            except OSError:
                pass
        else:
            orphaned += 1
    if orphaned:
        _log.warning("cleanup_reheal: %d flagged fact(s) not found verbatim in "
                     "shared (reworded by a compaction?) — capture files kept "
                     "for recovery; run BEFORE a recompact next time", orphaned)
    return {"checked": checked, "removed": removed,
            "capture_files_deleted": deleted, "orphaned": orphaned}
