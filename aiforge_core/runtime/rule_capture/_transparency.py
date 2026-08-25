"""Capture pre-filter + actionable backstop, and the transparency UI surface:
list / undo / rescope of captured items (reversing storage + revoking gate flags).

Split out of the former single ``rule_capture`` module VERBATIM.
"""
from __future__ import annotations

import re
from pathlib import Path

from ._base import (
    _LOCK,
    _SESSION_ITEMS,
    _VALID_SCOPES,
    _file_lock,
    _index_path,
    _load_index,
    _save_index,
    log,
)
from ._gates import _clear_applied_flags, recognize_gate_intent, set_gate_flag
from ._store import _do_store


# ─────────────────────────── capture pre-filter ─────────────────────

_GREETINGS = {
    "hi", "hii", "hey", "hello", "yo", "sup", "thanks", "thank you",
    "thx", "ok", "okay", "k", "cool", "nice", "great", "yes", "no",
    "yep", "nope", "sure", "got it", "good", "perfect", "done",
    "thanks so much", "thank you so much", "thanks a lot",
}
# The preference/directive cue gate now lives in ONE place shared by every
# capture path (rule_capture, preference_capture, chat_learner).
from aiforge_core.runtime.capture_cues import has_cue as _has_cue


def should_classify(message: str) -> bool:
    """Cheap deterministic pre-filter: should this message even reach the LLM
    classifier? Skips short messages, pure greetings/acks, and messages with NO
    preference/directive cue — so an ordinary turn ("hi", "fix the bug") never
    pays the classify cost."""
    m = (message or "").strip()
    if len(m) < 12:
        return False
    low = m.lower().strip(" .!?")
    if low in _GREETINGS:
        return False
    if _has_cue(m):
        return True
    # Deterministic gate-intent (e.g. "commit directly, the machine has access")
    # is also a worthwhile cue even without a keyword above.
    return recognize_gate_intent(m, category="rule") is not None


# Imperative BUILD verbs that, with a "now"/"then" cadence, mean a real task is
# present — the backstop that FORCES the agent to run even if the classifier
# (wrongly) said task_present=false. Never drop a real task.
_ACTIONABLE_VERB_RE = re.compile(
    r"\b(fix|add|create|implement|build|write|change|update|refactor|run|"
    r"delete|remove)\b", re.IGNORECASE)
_ACTIONABLE_TIME_RE = re.compile(r"\b(now|then)\b", re.IGNORECASE)


def looks_actionable(message: str) -> bool:
    """True when the message carries an imperative build verb alongside a
    'now'/'then' cadence (e.g. "...and now fix the bug") — a deterministic
    backstop so a combined rule+task message is never short-circuited as
    pure-capture."""
    m = message or ""
    return bool(_ACTIONABLE_VERB_RE.search(m) and _ACTIONABLE_TIME_RE.search(m))


# ─────────────────────────── transparency: list/rescope/undo ────────

def _visible_to(item: dict, repo: str | None) -> bool:
    """Global items always; a PROJECT item only for its own repo."""
    if item.get("undone"):
        return False
    return not (repo is not None and item.get("scope") == "project"
                and item.get("repo") != repo)


def list_captured(repo: str | None = None, session_id=None) -> list[dict]:
    """Captured items for the transparency UI. Global items always; project
    items filtered by ``repo`` when given; plus this session's ephemeral
    items."""
    out: list[dict] = []
    try:
        out = [it for it in _load_index().get("items", {}).values()
               if _visible_to(it, repo)]
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.list_captured failed: %s", exc)
    if session_id is not None:
        with _LOCK:
            out.extend(list(_SESSION_ITEMS.get(str(session_id), [])))
    return out


def _remove_storage(item: dict) -> None:
    """Best-effort reversal of an item's storage side effects."""
    # md_store bullet
    src = item.get("md_source")
    line = item.get("md_bullet")
    if src and line:
        try:
            from aiforge_core.memory import md_store
            p = md_store._find_by_source(src)
            if p is not None:
                kept = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                        if ln.strip() != line.strip()]
                p.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("rule_capture undo md bullet failed: %s", exc)
    # repo rule file
    rp = item.get("rule_path")
    if rp:
        try:
            Path(rp).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("rule_capture undo rule file failed: %s", exc)


def undo(rid: str) -> bool:
    """Remove a captured item (reverses md/repo-rule storage best-effort;
    REVOKES any gate flag it enabled so a deleted gate-disabling rule re-enables
    the gate). True when an item was found."""
    # Session items first.
    with _LOCK:
        for sid, items in _SESSION_ITEMS.items():
            for i, it in enumerate(items):
                if it.get("id") == rid:
                    found = items.pop(i)
                    break
            else:
                continue
            break
        else:
            found = None
    if found is not None:
        _clear_applied_flags(found)            # gate re-enabled
        return True
    idx = _load_index()
    item = idx.get("items", {}).get(rid)
    if not item:
        return False
    _clear_applied_flags(item)                 # gate re-enabled
    _remove_storage(item)
    with _LOCK, _file_lock(_index_path()):
        idx = _load_index()
        if rid in idx.get("items", {}):
            del idx["items"][rid]
            _save_index(idx)
    return True


def _find_item(rid: str) -> tuple[dict | None, bool]:
    """``(item, in_session)`` — the captured item, from the session store or the
    persistent index."""
    with _LOCK:
        for items in _SESSION_ITEMS.values():
            for it in items:
                if it.get("id") == rid:
                    return dict(it), True
    it = _load_index().get("items", {}).get(rid)
    return (dict(it) if it else None), False


def _drop_session_item(rid: str) -> None:
    with _LOCK:
        for items in _SESSION_ITEMS.values():
            for i, it in enumerate(list(items)):
                if it.get("id") == rid:
                    items.pop(i)
                    return


def _drop_indexed_item(found: dict, rid: str) -> None:
    _remove_storage(found)
    with _LOCK, _file_lock(_index_path()):
        idx = _load_index()
        idx.get("items", {}).pop(rid, None)
        _save_index(idx)


def rescope(rid: str, new_scope: str, *, repo_root: str | None = None) -> dict:
    """Re-file a captured item under a new scope, preserving its id. Also MOVES
    any gate flag it enabled to the new scope (clear old, set new) so the
    gate-disable follows the rule. ``repo_root`` is threaded so a global→project
    rescope actually writes ``<repo>/.aiforge/rules/``."""
    new_scope = (new_scope or "").strip().lower()
    if new_scope not in _VALID_SCOPES:
        return {"ok": False, "error": f"invalid scope: {new_scope}"}
    found, in_session = _find_item(rid)
    if found is None:
        return {"ok": False, "error": "not found"}
    if found.get("scope") == new_scope:
        return {"id": rid, "scope": new_scope,
                "category": found.get("category")}

    old_flags = list(found.get("applied_flags") or [])
    # Remove the old storage + clear old gate flags, then re-store under the new
    # scope with the same id.
    _clear_applied_flags(found)
    if in_session:
        _drop_session_item(rid)
    else:
        _drop_indexed_item(found, rid)
    res = _do_store({"category": found.get("category"), "scope": new_scope,
                     "canonical": found.get("canonical")},
                    rid=rid, repo=found.get("repo"),
                    session_id=found.get("session_id"), repo_root=repo_root)
    # Move each gate flag to the new scope (recorded onto the new item). A move
    # to global is refused unless explicitly confirmed → the gate stays enabled.
    for entry in old_flags:
        set_gate_flag(entry.get("name"), scope=new_scope,
                      repo=found.get("repo"),
                      session_id=found.get("session_id"), rule_id=rid)
    return {"id": rid, "scope": new_scope, "category": found.get("category"),
            "location": res.get("location")}
