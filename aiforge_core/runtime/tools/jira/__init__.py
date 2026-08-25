"""Jira (Server / Data Center) tool — search / read / create / update issues.

Lets the chat agent pull an issue in, analyse it, file a new one, edit fields,
or drop a comment. Server/DC REST API v2 (``/rest/api/2``).

Config (env):
  JIRA_BASE_URL   e.g. https://jira.internal  (no trailing /)
  JIRA_TOKEN      Personal Access Token (Bearer) — or the password/token
                  for basic auth when JIRA_USER is also set
  JIRA_USER       (optional) username/email → switches to Basic auth
  JIRA_INSECURE_TLS=1   skip TLS verify for a self-signed internal cert

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
    _auth_scheme,
    _base,
    _BODY_CAP,
    _conf,
    _configured,
    _headers,
    _http,
    _issue_url,
    _request,
    _search_cap,
    _SEARCH_PAGE,
    _ssl_ctx,
    _TIMEOUT_S,
    _truthy,
    default_project,
)
from ._format import _fmt_secs, _issue_summary, _time_fields, _TIME_FIELDS
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


def jira_search(args: dict, cwd: str | None = None) -> dict:
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


def jira_read(args: dict, cwd: str | None = None) -> dict:
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


def jira_worklog(args: dict, cwd: str | None = None) -> dict:
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


def jira_log_work(args: dict, cwd: str | None = None) -> dict:
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


def jira_create(args: dict, cwd: str | None = None) -> dict:
    """Create an issue. Required: ``project`` (key), ``summary``. Optional:
    ``issuetype`` (name, default 'Task'), ``description``, ``priority`` (name),
    ``labels`` (list), ``assignee`` (name), ``parent`` (key, for sub-tasks)."""
    if not args.get("project") and default_project():
        args = {**args, "project": default_project()}
    for k in ("project", "summary"):
        if not args.get(k):
            return {"ok": False, "error": f"missing '{k}'"}
    fields: dict = {
        "project": {"key": args["project"]},
        "summary": args["summary"],
        "issuetype": {"name": args.get("issuetype") or "Task"},
    }
    if args.get("description"):
        fields["description"] = to_jira_wiki(str(args["description"]))
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}
    if args.get("assignee"):
        fields["assignee"] = {"name": args["assignee"]}
    if args.get("labels"):
        labels = args["labels"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]
        fields["labels"] = labels
    if args.get("parent"):
        fields["parent"] = {"key": str(args["parent"])}
    r = _request("POST", "/rest/api/2/issue", body={"fields": fields})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    key = d.get("key", "")
    return {"ok": True, "key": key, "url": _issue_url(key),
            "written": {"summary": args.get("summary"),
                        "description": args.get("description")}}


def _wanted_status(args: dict, raw_fields: dict) -> str:
    """Jira status is NOT an editable field — it changes only via a workflow
    transition. A ``status``/``state`` arg (or a ``status`` inside ``fields``)
    is routed to jira_transition, so "move CLR-1 to In Progress" works whether
    the agent calls jira_update or jira_transition."""
    want = (args.get("status") or args.get("state") or "").strip()
    if want or raw_fields.get("status") is None:
        return want
    st = raw_fields.pop("status")
    return (st.get("name") if isinstance(st, dict) else str(st)).strip()


def _update_fields(args: dict, raw_fields: dict) -> dict:
    fields: dict = {}
    if args.get("summary"):
        fields["summary"] = args["summary"]
    if args.get("description") is not None:
        fields["description"] = to_jira_wiki(str(args["description"]))
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}
    if args.get("assignee"):
        fields["assignee"] = {"name": args["assignee"]}
    if args.get("labels") is not None:
        labels = args["labels"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]
        fields["labels"] = labels
    if raw_fields:                       # status already popped out
        fields.update(raw_fields)
    return fields


def jira_update(args: dict, cwd: str | None = None) -> dict:
    """Update issue fields. Required: ``key``. Provide any of ``summary``,
    ``description``, ``priority`` (name), ``labels`` (list), ``assignee``
    (name), ``status`` (auto-routed to a workflow transition), or a raw
    ``fields`` dict (merged last, wins)."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    raw_fields = dict(args["fields"]) if isinstance(args.get("fields"), dict) else {}
    status_want = _wanted_status(args, raw_fields)
    transitioned = None
    if status_want:
        tr = jira_transition({"key": key, "transition": status_want,
                              "comment": args.get("comment")}, cwd)
        if not tr.get("ok"):
            return tr
        transitioned = status_want
    fields = _update_fields(args, raw_fields)
    if not fields:
        # A status-only change is legit (it went through the transition above).
        if transitioned:
            return {"ok": True, "key": key, "status": transitioned,
                    "transitioned": True, "url": _issue_url(key)}
        return {"ok": False, "error": "no fields to update"}
    r = _request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                 body={"fields": fields})
    if not r["ok"]:
        return r
    written = {k: args[k] for k in ("summary", "description", "priority",
               "labels", "assignee") if args.get(k) is not None}
    if transitioned:
        written["status"] = transitioned
    return {"ok": True, "key": key, "url": _issue_url(key),
            "transitioned": bool(transitioned), "written": written}


def jira_comments(args: dict, cwd: str | None = None) -> dict:
    """READ every comment on an issue by ``key`` — author, body, timestamps.

    Exists because the only comment tool used to be the WRITE one
    (``jira_comment``), so "show me the comments on ENG-123" had no matching
    read tool and the model reached for the poster instead. Paginated newest
    call-order; ``limit`` caps the page (default 50).
    """
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    try:
        limit = max(1, min(100, int(args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                 params={"maxResults": limit})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    rows = []
    for c in (data.get("comments") or []):
        rows.append({
            "id": c.get("id"),
            "author": ((c.get("author") or {}) or {}).get("displayName"),
            "created": c.get("created"),
            "updated": c.get("updated"),
            "body": (c.get("body") or "")[:4000],
        })
    total = data.get("total")
    return {"ok": True, "key": key, "comments": rows, "count": len(rows),
            "total": total,
            "truncated": isinstance(total, int) and total > len(rows),
            "url": _issue_url(key)}


def jira_comment(args: dict, cwd: str | None = None) -> dict:
    """WRITE a NEW comment onto an issue. Required: ``key``, ``body``.

    To READ existing comments use ``jira_comments`` (plural) — this posts.
    """
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    # Server/DC v2 renders WIKI markup — convert the agent's HTML/Markdown body
    # so it doesn't post with literal <p>/<strong>/## tags.
    r = _request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                 body={"body": to_jira_wiki(str(args["body"]))})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "key": key, "id": d.get("id"), "url": _issue_url(key),
            "written": {"comment": str(args["body"])[:2000]}}


def jira_transitions(args: dict, cwd: str | None = None) -> dict:
    """List the workflow transitions currently available for an issue
    (id + name + target status). Required: ``key``."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    trs = [{"id": t.get("id"), "name": t.get("name"),
            "to": (t.get("to") or {}).get("name")}
           for t in (d.get("transitions") or [])]
    return {"ok": True, "key": key, "transitions": trs}


def jira_transition(args: dict, cwd: str | None = None) -> dict:
    """Move an issue through its workflow (e.g. To Do → In Progress → Done).
    Required: ``key`` + ``transition`` (a transition id, its name, or the target
    status name — matched case-insensitively). Optional ``comment``."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    want = str(args.get("transition") or args.get("to")
               or args.get("status") or args.get("name") or "").strip()
    if not want:
        return {"ok": False, "error": "missing 'transition' (name, id or status)"}
    lst = jira_transitions({"key": key})
    if not lst["ok"]:
        return lst
    tid = None
    for t in lst["transitions"]:
        if (str(t.get("id")) == want
                or (t.get("name") or "").lower() == want.lower()
                or (t.get("to") or "").lower() == want.lower()):
            tid = t.get("id")
            break
    if tid is None:
        return {"ok": False, "error": f"no transition matching '{want}'",
                "available": [t.get("name") for t in lst["transitions"]]}
    body: dict = {"transition": {"id": tid}}
    if args.get("comment"):
        body["update"] = {"comment": [{"add": {"body": args["comment"]}}]}
    r = _request("POST",
                 f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions",
                 body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "transitioned_to": want,
            "url": _issue_url(key)}


def jira_assign(args: dict, cwd: str | None = None) -> dict:
    """Assign an issue to a user. Required: ``key``, ``assignee`` (username;
    ``"-1"`` / ``"unassigned"`` clears the assignee)."""
    key = (args.get("key") or args.get("id") or "").strip()
    who = str(args.get("assignee") or args.get("user") or "").strip()
    if not key:
        return {"ok": False, "error": _MISSING_KEY}
    if not who:
        return {"ok": False, "error": "missing 'assignee'"}
    name = None if who.lower() in ("-1", "unassigned", "none", "") else who
    r = _request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}/assignee",
                 body={"name": name})
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "assignee": name or "(unassigned)",
            "url": _issue_url(key)}


def jira_link_issues(args: dict, cwd: str | None = None) -> dict:
    """Link two issues. Required: ``inward`` + ``outward`` (issue keys) and
    ``type`` (link-type name, e.g. 'Blocks', 'Relates', 'Duplicate'). Semantics:
    inward <type> outward (e.g. inward BLOCKS outward)."""
    inward = str(args.get("inward") or args.get("from") or "").strip()
    outward = str(args.get("outward") or args.get("to") or "").strip()
    ltype = str(args.get("type") or args.get("link_type") or "Relates").strip()
    if not inward or not outward:
        return {"ok": False, "error": "need 'inward' + 'outward' issue keys"}
    body: dict = {"type": {"name": ltype},
                  "inwardIssue": {"key": inward},
                  "outwardIssue": {"key": outward}}
    if args.get("comment"):
        body["comment"] = {"body": str(args["comment"])}
    r = _request("POST", "/rest/api/2/issueLink", body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "linked": {"inward": inward, "outward": outward,
                                   "type": ltype}}


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
    if err.startswith("http 401") or err.startswith("http 403"):
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
