"""Note curator — the learner pass over managed workspace notes.

A dossier note (work/jira/<KEY>/ticket.md, work/confluence/<id>/page.md, …)
is written once at read time and then drifts: the ticket moves status, gets
reassigned, links die. The curator re-verifies a note against its live source
and RECORDS what changed, instead of silently rewriting history:

  - Facts (status/assignee/priority/title) are refreshed in place;
  - every change is appended as a dated line under ``## Learnings``
    (``- 2026-07-10: status To Do → In Progress (auto-curated)``) so the note
    keeps its own audit trail;
  - dead links get a ``(dead)`` suffix — flagged, never deleted (the URL is
    still evidence of where the info came from);
  - ``updated_at`` is bumped even when nothing changed, so the staleness
    check doesn't re-fire every turn.

Path jail: the curator only ever touches files under the managed work root
(``work_context._root()``) — it is exposed as an ungated chat tool, so the
path check is the security boundary, not the tool policy.

Soft-error contract: ``curate_note`` returns ``{"ok", "updated", "changes"}``
and NEVER raises — an unconfigured Jira, a network blip, a hand-mangled note
all degrade to "no change", not an exception in the chat turn.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import urllib.error
import urllib.parse
import urllib.request

_DEAD_SUFFIX = " (dead)"
# Only a DEFINITIVE gone (404/410) marks a link dead. Timeouts / DNS blips /
# 5xx are transient — an offline laptop must not mass-flag a note's links.
_DEAD_STATUSES = (404, 410)
_LINK_TIMEOUT_S = 3


def _stale_hours() -> float:
    """Threshold before a bound note is auto-re-verified. Env-gated;
    <= 0 disables staleness curation entirely."""
    try:
        return float(os.environ.get("AIFORGE_NOTE_STALE_HOURS", "24"))
    except (TypeError, ValueError):
        return 24.0


def is_stale(updated_at_iso: str, *, now: _dt.datetime | None = None,
             hours: float | None = None) -> bool:
    """True when ``updated_at_iso`` is older than the staleness threshold.
    A MISSING/unparseable stamp is NOT stale — legacy/hand-made files without
    frontmatter must not be churned by the auto-curator."""
    h = _stale_hours() if hours is None else hours
    if h <= 0 or not updated_at_iso:
        return False
    try:
        ts = _dt.datetime.fromisoformat(str(updated_at_iso))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.UTC)
    now = now or _dt.datetime.now(_dt.UTC)
    return (now - ts) >= _dt.timedelta(hours=h)


def _work_root() -> str:
    from aiforge_core.runtime import work_context
    return os.path.realpath(work_context._root())


def _inside_work_root(path: str) -> bool:
    try:
        return os.path.realpath(path).startswith(_work_root() + os.sep)
    except (OSError, ValueError):
        return False


def primary_note_for_cwd(cwd: str | None) -> str | None:
    """The bound context's primary note (ticket.md / page.md), if the cwd IS a
    managed jira/confluence context folder and the note exists."""
    from aiforge_core.runtime import work_context
    ctx = work_context.context_for_path(cwd)
    if not ctx or ctx[0] not in ("jira", "confluence"):
        return None
    name = "ticket.md" if ctx[0] == "jira" else "page.md"
    path = os.path.join(work_context.context_dir(*ctx, create=False), name)
    return path if os.path.isfile(path) else None


def stale_note_path(cwd: str | None) -> str | None:
    """Cheap, no-network pre-check for the chat-turn hook: the bound context's
    note path IFF it exists and its ``updated_at`` crossed the threshold."""
    try:
        path = primary_note_for_cwd(cwd)
        if not path:
            return None
        from aiforge_core.runtime import work_notes
        with open(path, encoding="utf-8") as fh:
            parsed = work_notes.parse_note(fh.read())
        stamp = str(parsed["frontmatter"].get("updated_at") or "")
        return path if is_stale(stamp) else None
    except Exception:  # noqa: BLE001 — a pre-check must never break a turn
        return None


def _fact_map(facts: list[str]) -> dict[str, str]:
    """'status: In Progress' lines → {'status': 'In Progress'}. Non 'k: v'
    lines are keyed by themselves so they survive a rewrite untouched."""
    out: dict[str, str] = {}
    for f in facts or []:
        k, sep, v = str(f).partition(":")
        if sep and k.strip():
            out[k.strip().lower()] = v.strip()
    return out


def _live_facts(kind: str, key: str) -> dict | None:
    """Re-fetch the source entity via the existing integration tools (their
    soft-error contract included). None = source not reachable/configured —
    which means 'leave the facts alone', not an error."""
    try:
        if kind == "jira":
            from aiforge_core.runtime.tools import jira
            r = jira.jira_read({"key": key, "attachments": "false"}, None)
            if not r.get("ok"):
                return None
            return {"status": r.get("status") or "",
                    "assignee": r.get("assignee") or "",
                    "priority": r.get("priority") or "",
                    "title": r.get("summary") or ""}
        if kind == "confluence":
            from aiforge_core.runtime.tools import confluence
            r = confluence.confluence_read(
                {"id": key, "attachments": "false"}, None)
            if not r.get("ok"):
                return None
            return {"title": r.get("title") or "",
                    "version": str(r.get("version") or "")}
    except Exception:  # noqa: BLE001
        return None
    return None


def _sanctioned_host(url: str) -> bool:
    """Links back to the configured Jira/Confluence base are intranet targets
    the operator already sanctioned by configuring the integration."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
        for mod in ("jira", "confluence"):
            from importlib import import_module
            m = import_module(f"aiforge_core.runtime.tools.{mod}")
            base = urllib.parse.urlsplit(m._base() or "").hostname or ""
            if base and host == base:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _link_dead(url: str) -> bool:
    """Best-effort HEAD; True only on a definitive 404/410. Egress-gated the
    same way the rest of the system is: external hosts need
    AIFORGE_ALLOW_WEB_FETCH; the configured jira/confluence host is exempt.
    AIFORGE_NOTE_LINK_CHECK=0 disables all link probing."""
    if os.environ.get("AIFORGE_NOTE_LINK_CHECK", "1").strip().lower() \
            in ("0", "false", "no", "off"):
        return False
    if not _sanctioned_host(url):
        try:
            from aiforge_core.runtime.tools import web_search as _ws
            if not _ws._fetch_allowed():
                return False
        except Exception:  # noqa: BLE001
            return False
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "AIForgeCrew-NoteCurator/1.0"})
        with urllib.request.urlopen(req, timeout=_LINK_TIMEOUT_S) as resp:
            return getattr(resp, "status", 200) in _DEAD_STATUSES
    except urllib.error.HTTPError as exc:
        return exc.code in _DEAD_STATUSES
    except Exception:  # noqa: BLE001 — transient network trouble ≠ dead link
        return False


def _today() -> str:
    return _dt.date.today().isoformat()


def curate_note(path: str, cwd: str | None = None) -> dict:
    """Re-verify one managed note against its live source.

    Returns ``{"ok": bool, "updated": bool, "changes": [str, ...]}``.
    NEVER raises."""
    try:
        return _curate(path)
    except Exception as exc:  # noqa: BLE001 — hard soft-error boundary
        return {"ok": False, "updated": False, "changes": [],
                "error": str(exc)}


def _curate(path: str) -> dict:
    # Path jail FIRST — this runs as an ungated chat tool, so the work-root
    # containment check is the only thing between it and arbitrary files.
    if not path or not _inside_work_root(path):
        return {"ok": False, "updated": False, "changes": [],
                "error": "path outside the managed work root — refusing"}
    if not os.path.isfile(path):
        return {"ok": False, "updated": False, "changes": [],
                "error": f"no note at {path}"}
    from aiforge_core.runtime import work_context, work_notes
    with open(path, encoding="utf-8") as fh:
        parsed = work_notes.parse_note(fh.read())
    fm = parsed["frontmatter"]
    sec = parsed["sections"]
    kind = str(fm.get("kind") or "")
    key = str(fm.get("key") or "")
    if not kind or not key:
        # Legacy note without frontmatter — derive identity from its folder.
        ctx = work_context.context_for_path(os.path.dirname(path))
        if ctx:
            kind, key = kind or ctx[0], key or ctx[1]

    changes: list[str] = []
    updates: dict = {}
    facts = list(sec.get("facts") or [])
    learnings = list(sec.get("learnings") or [])

    # ── Fact drift vs the live source (jira/confluence only, best-effort).
    live = _live_facts(kind, key) if kind in ("jira", "confluence") else None
    if live:
        have = _fact_map(facts)
        for field, new in live.items():
            if field == "title":
                continue        # handled below against the H1, not Facts
            old = have.get(field, "")
            if new and new != old:
                changes.append(f"{field} {old or '(unset)'} → {new}")
                # rewrite (or add) the fact line in place, order preserved
                pat = re.compile(rf"^{re.escape(field)}\s*:", re.IGNORECASE)
                for i, f in enumerate(facts):
                    if pat.match(str(f)):
                        facts[i] = f"{field}: {new}"
                        break
                else:
                    facts.append(f"{field}: {new}")
        new_title = live.get("title") or ""
        old_title = parsed["title"]
        # ticket titles render as "KEY — summary"; only diff the summary part
        plain_old = old_title.split("—", 1)[-1].strip() if old_title else ""
        if new_title and plain_old and new_title != plain_old \
                and new_title != old_title:
            changes.append(f"title {plain_old} → {new_title}")
            updates["title"] = old_title.replace(plain_old, new_title, 1)
    if facts != list(sec.get("facts") or []):
        updates["facts"] = facts

    # ── Dead-link flagging: suffix, never delete (the ref stays as evidence).
    links = list(sec.get("links") or [])
    flagged = []
    for lk in links:
        s = str(lk)
        # "[" covers both cross-ref forms — canonical relative md file links
        # ([kind/key](../../…/ticket.md)) and legacy [[kind/key]] wiki refs:
        # local refs are the curator's own filesystem, not HTTP targets.
        if s.endswith(_DEAD_SUFFIX) or s.startswith("["):
            flagged.append(s)
            continue
        if re.match(r"^https?://", s, re.IGNORECASE) and _link_dead(s):
            flagged.append(s + _DEAD_SUFFIX)
            changes.append(f"link dead: {s}")
        else:
            flagged.append(s)
    if flagged != links:
        # a "(dead)"-suffixed entry still starts with http(s):// so it passes
        # normalize_links' scheme filter and is persisted verbatim.
        updates["links"] = flagged

    for c in changes:
        learnings.append(f"{_today()}: {c} (auto-curated)")
    if changes:
        updates["learnings"] = learnings

    # Always write: even a no-change pass bumps updated_at so staleness
    # doesn't re-trigger the curator on every subsequent turn.
    res = work_notes.update_note(path, **updates)
    if not res.get("ok"):
        return {"ok": False, "updated": False, "changes": changes,
                "error": res.get("error", "write failed")}
    return {"ok": True, "updated": bool(changes), "changes": changes}


__all__ = ["curate_note", "is_stale", "stale_note_path",
           "primary_note_for_cwd"]
