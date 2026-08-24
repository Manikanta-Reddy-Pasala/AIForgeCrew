"""Routing a classification to its store by category × scope (md_store / repo
rules / AiForgeMemory / in-session store) + repo-rule file writing.

Split out of the former single ``rule_capture`` module VERBATIM.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from ._base import (
    _LOCK,
    _SESSION_ITEMS,
    _file_lock,
    _index_path,
    _load_index,
    _save_index,
    log,
)


# ─────────────────────────── store ──────────────────────────────────

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "rule").lower()).strip("-")
    return (s or "rule")[:60]


def _write_repo_rule(repo_root: str, name: str, body: str,
                     triggers: list[str] | None = None) -> str | None:
    """Best-effort write of a Cursor-style rule into ``<repo_root>/.aiforge/
    rules/<slug>.md`` so the ticket/doer repo_rules pipeline honors it too.
    ``triggers`` (if any) makes the rule topic-gated instead of always-on.
    Returns the path written, or None on any failure."""
    try:
        d = Path(repo_root).expanduser() / ".aiforge" / "rules"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(name)}.md"
        trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
        front = "---\n" + f"name: {name}\n"
        if trig:
            front += "triggers: [" + ", ".join(trig) + "]\n"
        else:
            front += "alwaysApply: true\n"
        front += "---\n\n"
        path.write_text(front + body.strip() + "\n", encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture repo-rule write failed: %s", exc)
        return None


def _session_add(session_id, item: dict) -> None:
    if session_id is None:
        return
    with _LOCK:
        _SESSION_ITEMS.setdefault(str(session_id), []).append(item)


def _new_item(rid: str, c: dict, repo, session_id) -> dict:
    return {
        "id": rid, "category": c.get("category"), "scope": c.get("scope"),
        "canonical": (c.get("canonical") or "").strip(), "repo": repo,
        "session_id": (str(session_id) if session_id is not None else None),
        "location": "", "md_source": None, "md_bullet": None,
        "rule_path": None, "undone": False, "triggers": [],
        # Gate-disable flags this captured item explicitly enabled (via an
        # opt-in pill). Recorded here so undo/rescope/delete can REVOKE them —
        # a deleted gate-disabling rule must re-enable the gate.
        "applied_flags": [],
    }


def _rule_brief_source(scope: str, repo) -> tuple[str, str]:
    if scope == "global":
        return "rules:global", "AIForge rules (all sessions)"
    r = repo or "project"
    return f"rules:{r}", f"{r} — rules"


def _write_global_repo_rule(item: dict, canonical: str, triggers) -> None:
    """Also land GLOBAL rules in the canonical repo_rules store
    (~/.aiforge/rules) — the SAME store the Library UI + the ticket/doer
    pipeline read — so a directive captured in passing shows up alongside
    remember_rule / Library-form rules instead of living only in md_store
    (invisible to the Library)."""
    try:
        from aiforge_core.runtime import repo_rules as _rr
        res = _rr.write_rule(canonical[:60] or "rule", canonical,
                             globs=(triggers or None), always=not triggers)
        if res.get("ok"):
            item["rule_path"] = res.get("path")
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("rule_capture global repo-rule write failed: %s", exc)


def _store_rule(item: dict, c: dict, repo, repo_root) -> None:
    """A rule/feedback item: a bullet in the scope's brief, plus the canonical
    repo_rules file for real rules."""
    from aiforge_core.memory import md_store
    cat, scope = item["category"], item["scope"]
    canonical = item["canonical"]
    triggers = c.get("triggers") or []
    item["triggers"] = triggers
    src, title = _rule_brief_source(scope, repo)
    bullet_text = (f"[triggers: {', '.join(triggers)}] {canonical}"
                   if triggers else canonical)
    md_store.append_bullet(source=src, title=title, bullet=bullet_text,
                           kind=cat, tags=[cat, scope])
    item["md_source"] = src
    item["md_bullet"] = "- " + bullet_text
    item["location"] = f"md:{src}"
    if cat != "rule":
        return
    if scope == "project" and repo_root:
        rp = _write_repo_rule(repo_root, canonical[:60] or "rule", canonical,
                              triggers=triggers)
        if rp:
            item["rule_path"] = rp
    elif scope == "global":
        _write_global_repo_rule(item, canonical, triggers)


def _store_memory(item: dict, repo) -> None:
    from aiforge_core.runtime.tools.memory_write import memory_write
    memory_write(text=item["canonical"], kind="note",
                 tags=[item["category"], item["scope"]], repo=repo or "notes")
    item["location"] = f"memory:{item['scope']}"


def _do_store(c: dict, *, rid: str, repo: str | None, session_id,
              repo_root: str | None) -> dict:
    item = _new_item(rid, c, repo, session_id)
    cat, scope = item["category"], item["scope"]
    if scope == "session":
        item["location"] = "session"
        _session_add(session_id, item)
        return {"id": rid, "location": "session", "category": cat,
                "scope": scope}
    try:
        if cat in ("rule", "feedback"):
            _store_rule(item, c, repo, repo_root)
        else:
            _store_memory(item, repo)
    except Exception as exc:  # noqa: BLE001 — store must never raise
        log.debug("rule_capture store soft-fail: %s", exc)
    # Persist the index entry (global/project only — session is in-memory).
    with _LOCK, _file_lock(_index_path()):
        idx = _load_index()
        idx["items"][rid] = item
        _save_index(idx)
    return {"id": rid, "location": item["location"], "category": cat,
            "scope": scope}


def store(c: dict, *, repo: str | None = None, session_id=None,
          repo_root: str | None = None) -> dict:
    """Route a classification to its store by category × scope. Generates a
    stable uuid id. Never raises — soft-fails and logs."""
    rid = uuid.uuid4().hex
    try:
        return _do_store(c, rid=rid, repo=repo, session_id=session_id,
                         repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.store failed: %s", exc)
        return {"id": rid, "location": "", "category": c.get("category"),
                "scope": c.get("scope")}
