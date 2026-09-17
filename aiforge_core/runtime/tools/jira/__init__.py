"""Jira (Server / Data Center) tool — search / read / create / update issues.

Lets the chat agent pull an issue in, analyse it, file a new one, edit fields,
or drop a comment. Server/DC REST API v2 (``/rest/api/2``).

Config (env):
  JIRA_BASE_URL   e.g. https://jira.internal  (no trailing /)
  JIRA_TOKEN      Personal Access Token (Bearer) — or the password/token
                  for basic auth when JIRA_USER is also set
  JIRA_USER       (optional) username/email → switches to Basic auth
  JIRA_CA_BUNDLE=/path/ca.pem   trust an internal CA — verification STAYS ON
  JIRA_INSECURE_TLS=0   force TLS verify on (DEFAULT is to SKIP it: the auth
                         token travels over an unauthenticated channel)

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop.

This module was split (grouped by concern) into ``_core`` / ``_format`` /
``_attachments`` / ``_projects`` submodules; the issue CRUD/workflow tools stay
in this package body (so ``jira._request`` / ``jira._SEARCH_PAGE`` patch points
resolve identically). The package re-exports the full former public surface so
``from aiforge_core.runtime.tools import jira`` and every ``jira.<name>``
attribute access is unchanged.
"""
from __future__ import annotations

import urllib.parse

from aiforge_core.runtime.tools.jira_format import to_jira_wiki

from ._attachments import _fetch_attachments, _max_images, _save_attachment
from ._core import (
    _BODY_CAP,
    _SEARCH_PAGE,
    _TIMEOUT_S,
    _auth_scheme,
    _base,
    _conf,
    _configured,
    _headers,
    _http,
    _issue_url,
    _request,
    _search_cap,
    _ssl_ctx,
    _truthy,
    default_project,
)
from ._edit import (  # noqa: F401  # re-exported
    _update_fields,
    _wanted_status,
    jira_assign,
    jira_comment,
    jira_comments,
    jira_create,
    jira_link_issues,
    jira_transition,
    jira_transitions,
    jira_update,
)
from ._format import _TIME_FIELDS, _fmt_secs, _issue_summary, _time_fields
from ._projects import (
    jira_boards,
    jira_dashboard_create,
    jira_dashboard_read,
    jira_dashboards,
    jira_myself,
    jira_projects,
    jira_remote_links,
    jira_resolve_project,
    jira_sprint_issues,
    jira_sprints,
)

_MISSING_KEY = "missing 'key'"


# ─────────────────────────── tools ──────────────────────────────────

def _search_jql(args: dict) -> tuple[str, dict | None]:
    """``(jql, error)``.

    A bare ``text ~ …`` is SCOPED to the default project when the caller didn't
    name one — otherwise it searches every project the token can see, a common
    cause of a job's filter returning the wrong/empty set. An explicit
    ``project=`` in the JQL, or an explicit ``args["project"]``, wins.
    """
    jql = (args.get("jql") or "").strip()
    if not jql and args.get("query"):
        q = str(args["query"]).replace('"', '\\"')
        jql = f'text ~ "{q}" ORDER BY updated DESC'
    if not jql:
        return "", {"ok": False, "error": "missing 'query' or 'jql'"}
    proj = (args.get("project") or default_project() or "").strip().replace(
        '"', '\\"')
    if not proj or "project" in jql.lower():
        return jql, None
    low = jql.lower()
    if " order by" in low:
        i = low.index(" order by")
        return f'project = "{proj}" AND ({jql[:i]}){jql[i:]}', None
    return f'project = "{proj}" AND ({jql})', None


def _search_limit(args: dict) -> int:
    """The desired count. "all"/0/negative → everything up to the safety cap."""
    cap = _search_cap()
    raw = args.get("limit", 50)
    if str(raw).strip().lower() in ("all", "0", "-1", ""):
        return cap
    try:
        return min(max(1, int(raw)), cap)
    except (TypeError, ValueError):
        return min(50, cap)


def _search_start(args: dict) -> int:
    try:
        return max(0, int(args.get("startAt", args.get("start_at", 0)) or 0))
    except (TypeError, ValueError):
        return 0


def _search_page(jql: str, start: int, page: int, flds: str) -> dict:
    return _request("GET", "/rest/api/2/search",
                    params={"jql": jql, "startAt": start, "maxResults": page,
                            "fields": flds})


def jira_search(args: dict, _cwd: str | None = None) -> dict:
    """Find issues. ``jql`` (raw JQL) OR ``query`` (full-text).

    ``limit`` = how many to return (default 50). Pass ``limit="all"`` (or 0) to
    pull every match up to the safety cap (``AIFORGE_JIRA_SEARCH_CAP``, def 500).
    Results are paginated internally, so a limit above Jira's per-page cap works.
    The reply carries ``total`` (all matches) and ``truncated`` (more exist)."""
    jql, err = _search_jql(args)
    if err:
        return err
    # Opt-in time tracking on search hits (original/remaining estimate + spent).
    want_time = _truthy(str(args.get("time", args.get("with_time", "false"))))
    flds = "summary,status,issuetype,assignee" + (
        "," + _TIME_FIELDS if want_time else "")
    limit = _search_limit(args)
    start = _search_start(args)
    out: list = []
    total = None
    while len(out) < limit:
        r = _search_page(jql, start, min(_SEARCH_PAGE, limit - len(out)), flds)
        if not r["ok"]:
            # Fail hard on the first page; on a later page keep what we have.
            if not out:
                return r
            return {"ok": True, "results": out, "total": total,
                    "count": len(out), "truncated": True,
                    "error": r.get("error")}
        issues, total = _absorb_page(r, out, total, want_time)
        start += len(issues)
        # Exhausted: server returned a short/empty page, or we reached total.
        if not issues or (isinstance(total, int) and start >= total):
            break
    return {"ok": True, "results": out, "total": total, "count": len(out),
            "truncated": isinstance(total, int) and total > len(out)}


def _absorb_page(r: dict, out: list, total, want_time: bool) -> tuple[list, int | None]:
    """Append one page's issues to ``out``; returns ``(issues, total)``."""
    data = r["data"] if isinstance(r["data"], dict) else {}
    issues = data.get("issues") or []
    if isinstance(data.get("total"), int):
        total = data["total"]
    out.extend(_issue_summary(x, with_time=want_time) for x in issues)
    return issues, total


def _named(field) -> str | None:
    """The ``name``/``displayName`` of a Jira object field, tolerating None."""
    obj = field or {}
    return obj.get("name") or obj.get("displayName")


def _issue_comments(fields: dict) -> list[dict]:
    comment = (fields.get("comment") or {}) or {}
    return [{"author": _named(c.get("author")),
             "body": (c.get("body") or "")[:4000]}
            for c in (comment.get("comments") or [])]


def _issue_view(d: dict, fields: dict) -> dict:
    return {"ok": True, "key": d.get("key"), "summary": fields.get("summary"),
            "type": _named(fields.get("issuetype")),
            "status": _named(fields.get("status")),
            "assignee": _named(fields.get("assignee")),
            "reporter": _named(fields.get("reporter")),
            "priority": _named(fields.get("priority")),
            "labels": fields.get("labels") or [],
            "time": _time_fields(fields),
            "description": (fields.get("description") or "")[:_BODY_CAP],
            "comments": _issue_comments(fields),
            "url": _issue_url(d.get("key", ""))}


def jira_read(args: dict, _cwd: str | None = None) -> dict:
    """Read an issue by ``key`` (e.g. ENG-123). Returns fields + comments."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                 params={"fields": "summary,description,status,issuetype,"
                                   "assignee,reporter,priority,labels,comment,"
                                   "attachment," + _TIME_FIELDS})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    out = _issue_view(d, f)
    # Pull attachments (images + documents) + analyse them so the agent uses
    # them as part of the task (opt out with attachments=false). Best-effort.
    if _truthy(str(args.get("attachments", args.get("images", "true")))):
        # Save the ticket's attachments INTO its own folder (work/jira/<KEY>/)
        # so they persist across sessions — ticket-specific, not global.
        atts = _fetch_attachments(f.get("attachment") or [],
                                  save_ctx=("jira", d.get("key") or key))
        if atts:
            out["attachments"] = atts
    return out


def _worklog_rows(worklogs: list) -> tuple[list[dict], int]:
    """``(rows, total seconds)`` for the fetched worklog page."""
    rows = []
    total_secs = 0
    for w in worklogs:
        secs = w.get("timeSpentSeconds") or 0
        try:
            total_secs += int(secs)
        except (TypeError, ValueError):
            pass
        rows.append({"author": _named(w.get("author")),
                     "time_spent": w.get("timeSpent") or _fmt_secs(secs),
                     "time_spent_seconds": secs,
                     "started": w.get("started"),
                     "comment": (w.get("comment") or "")[:500]})
    return rows, total_secs


def _time_rollup(key: str) -> dict | None:
    """The issue's estimate/spent rollup — one extra lightweight call."""
    tr = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                  params={"fields": _TIME_FIELDS})
    if tr["ok"] and isinstance(tr["data"], dict):
        return _time_fields(tr["data"].get("fields") or {})
    return None


def jira_worklog(args: dict, _cwd: str | None = None) -> dict:
    """Read the time LOGGED against an issue by ``key`` — every worklog entry
    (who, how much, when, comment) plus the estimate/spent rollup. Answers
    "how much time has been recorded on ENG-123 and by whom"."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/worklog",
                 params={"maxResults": int(args.get("limit", 50))})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    logs, total_secs = _worklog_rows(data.get("worklogs") or [])
    total_available = data.get("total")
    return {"ok": True, "key": key, "worklogs": logs,
            "worklog_count": len(logs),
            "worklog_total": total_available,
            # NB: total_logged sums only the fetched page; when `truncated`, use
            # `tracking.time_spent` (the issue's authoritative rollup) instead.
            "truncated": isinstance(total_available, int)
                         and total_available > len(logs),
            "total_logged": _fmt_secs(total_secs),
            "total_logged_seconds": total_secs,
            "tracking": _time_rollup(key), "url": _issue_url(key)}


def jira_log_work(args: dict, _cwd: str | None = None) -> dict:
    """Record time against an issue. Required: ``key`` and ``time_spent``
    (Jira duration, e.g. '2h 30m' or '1d'). Optional: ``comment``, ``started``
    (ISO8601; defaults to server now)."""
    key = (args.get("key") or args.get("id") or "").strip()
    time_spent = (args.get("time_spent") or args.get("timeSpent")
                  or args.get("time") or "").strip()
    if not key or not time_spent:
        return {"ok": False, "error": "key and time_spent are required "
                                      "(e.g. time_spent='2h 30m')"}
    body: dict = {"timeSpent": time_spent}
    if args.get("comment"):
        body["comment"] = to_jira_wiki(str(args["comment"]))
    if args.get("started"):
        body["started"] = str(args["started"])
    r = _request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/worklog",
                 body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "logged": time_spent,
            "url": _issue_url(key)}


def jira_test() -> dict:
    """Connectivity + auth check for the Settings UI. Hits a cheap endpoint
    and, on auth failure, explains the most likely cause."""
    if not _configured():
        return {"ok": False, "error": "jira_not_configured"}
    scheme = _auth_scheme()
    r = _request("GET", "/rest/api/2/myself")
    if r.get("ok"):
        d = r["data"] if isinstance(r["data"], dict) else {}
        return {"ok": True, "base_url": _base(), "auth": scheme,
                "user": d.get("displayName") or d.get("name")}
    # Enrich auth errors with an actionable hint.
    err = str(r.get("error", ""))
    out = {**r, "auth": scheme, "base_url": _base()}
    if err.startswith(("http 401", "http 403")):
        if scheme == "basic":
            out["hint"] = ("Using BASIC auth (User field is filled). A Personal "
                           "Access Token must be sent as Bearer — clear the User "
                           "field to use the token directly. Only fill User for "
                           "username+password basic auth.")
        else:
            out["hint"] = ("Bearer/PAT rejected. Check the token is a Jira "
                           "Personal Access Token (not an API key/password), not "
                           "expired, and has read scope; and that Base URL has no "
                           "extra context path.")
    return out


__all__ = ["jira_search", "jira_read", "jira_create", "jira_update",
           "jira_comment", "jira_comments", "jira_link_issues", "jira_transitions", "jira_transition",
           "jira_assign", "jira_test"]
