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
"""
from __future__ import annotations

import base64
import os
import urllib.parse

from . import _http_integration as _http

_TIMEOUT_S = 20
_BODY_CAP = 200_000

_truthy = _http.truthy


def _conf() -> dict:
    """Resolve config via the shared integration helper: base_url/token/
    insecure_tls (insecure by default) + user + default_project."""
    return _http.integration_conf(
        "jira", "JIRA",
        str_fields=(("user", "JIRA_USER"),
                    ("default_project", "JIRA_DEFAULT_PROJECT")))


def default_project() -> str:
    return _conf().get("default_project") or ""


def _auth_scheme() -> str:
    return "basic" if _conf()["user"] else "bearer"


def _base() -> str:
    return _conf()["base_url"]


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _headers() -> dict[str, str]:
    c = _conf()
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "AIForgeCrew-Jira/1.0"}
    if c["user"]:
        h["Authorization"] = "Basic " + base64.b64encode(
            f"{c['user']}:{c['token']}".encode()).decode()
    else:
        h["Authorization"] = "Bearer " + c["token"]
    return h


def _ssl_ctx():
    return _http.ssl_context(_conf()["insecure_tls"])


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
    if not _configured():
        return {"ok": False, "error": "jira_not_configured",
                "hint": "set JIRA_BASE_URL + JIRA_TOKEN"}
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http.http_request(method, url, headers=_headers(), body=body,
                              timeout=_TIMEOUT_S, body_cap=_BODY_CAP,
                              context=_ssl_ctx(),
                              capture_headers=_http.ATLASSIAN_DENIED_HEADERS)


def _issue_url(key: str) -> str:
    return f"{_base()}/browse/{key}" if key else ""


def _max_images() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_INTEGRATION_MAX_IMAGES", "4")))
    except ValueError:
        return 4


def _fetch_attachments(attachments: list, role: str = "doer",
                       save_ctx: tuple | None = None) -> list[dict]:
    """Download issue attachments — images AND documents (pdf/xlsx/docx/text) —
    and analyse them (vision caption for images, extracted text for docs) so the
    agent can use them as part of the task. When ``save_ctx`` = (kind, key) is
    given, each file is also SAVED into that context's folder
    (~/.aiforge/work/<kind>/<key>/attachments/) so the ticket's images persist
    across sessions. Best-effort, capped, never raises."""
    cap = _max_images()
    if cap <= 0 or not attachments:
        return []
    from aiforge_core.runtime import chat_media
    save_dir = None
    if save_ctx:
        try:
            from aiforge_core.runtime import work_context as _wc
            save_dir = _wc.attachments_dir(save_ctx[0], save_ctx[1])
        except Exception:  # noqa: BLE001
            save_dir = None
    out: list[dict] = []
    for a in attachments:
        if len(out) >= cap:
            break
        if not isinstance(a, dict):
            continue
        mime = (a.get("mimeType") or "").lower()
        url = a.get("content")
        name = a.get("filename") or "attachment"
        if not url or not chat_media.supported_attachment(mime, name):
            continue
        try:
            import urllib.parse as _up
            got = _http.http_get_bytes(url, headers=_headers(),
                                       timeout=_TIMEOUT_S, context=_ssl_ctx(),
                                       allow_host=_up.urlsplit(_base()).hostname)
            if not got.get("ok"):
                out.append({"filename": name, "description": "",
                            "error": got.get("error")})
                continue
            info = chat_media.analyze_attachment(name, got["bytes"],
                                                 role, mime=mime)
            if save_dir:
                info["path"] = _save_attachment(save_dir, name, got["bytes"])
            out.append(info)
        except Exception as exc:  # noqa: BLE001
            out.append({"filename": name, "description": "", "error": str(exc)})
    return out


def _save_attachment(save_dir: str, name: str, raw: bytes) -> str:
    """Persist raw attachment bytes under ``save_dir`` (the ticket's folder).
    Returns the absolute path, or "" on failure — never raises."""
    import os as _os
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", name or "attachment").strip("_.") \
        or "attachment"
    try:
        path = _os.path.join(save_dir, safe)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path
    except OSError:
        return ""


def _fmt_secs(secs) -> str | None:
    """Jira-style 'Xh Ym' from a seconds count (None-safe)."""
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return "0m"
    # Jira convention: 1w=5d, 1d=8h.
    parts, units = [], (("w", 5 * 8 * 3600), ("d", 8 * 3600),
                        ("h", 3600), ("m", 60))
    for label, size in units:
        if s >= size:
            parts.append(f"{s // size}{label}")
            s %= size
    return " ".join(parts) or "0m"


def _time_fields(f: dict) -> dict:
    """Time-tracking view of an issue: original estimate, remaining, and time
    spent — both human ('2h 30m') and raw seconds. Reads the ``timetracking``
    object first (has the pretty strings), then the flat second-fields as a
    fallback. ``aggregate*`` include sub-tasks."""
    tt = f.get("timetracking") if isinstance(f.get("timetracking"), dict) else {}
    orig_s = tt.get("originalEstimateSeconds")
    if orig_s is None:
        orig_s = f.get("timeoriginalestimate")
    rem_s = tt.get("remainingEstimateSeconds")
    if rem_s is None:
        rem_s = f.get("timeestimate")
    spent_s = tt.get("timeSpentSeconds")
    if spent_s is None:
        spent_s = f.get("timespent")
    agg_spent = f.get("aggregatetimespent")
    return {
        "original_estimate": tt.get("originalEstimate") or _fmt_secs(orig_s),
        "remaining_estimate": tt.get("remainingEstimate") or _fmt_secs(rem_s),
        "time_spent": tt.get("timeSpent") or _fmt_secs(spent_s),
        "original_estimate_seconds": orig_s,
        "remaining_estimate_seconds": rem_s,
        "time_spent_seconds": spent_s,
        "aggregate_time_spent": _fmt_secs(agg_spent),
        "aggregate_time_spent_seconds": agg_spent,
    }


# Fields requested for the time-tracking view — reused by search + read.
_TIME_FIELDS = ("timetracking,timespent,timeoriginalestimate,timeestimate,"
                "aggregatetimespent")


def _issue_summary(d: dict, *, with_time: bool = False) -> dict:
    """Compact one-line view of an issue search hit."""
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    out = {
        "key": d.get("key"),
        "summary": f.get("summary"),
        "type": ((f.get("issuetype") or {}) or {}).get("name"),
        "status": ((f.get("status") or {}) or {}).get("name"),
        "assignee": ((f.get("assignee") or {}) or {}).get("displayName"),
    }
    if with_time:
        out["time"] = _time_fields(f)
    return out


# ─────────────────────────── tools ──────────────────────────────────

def jira_search(args: dict, cwd: str | None = None) -> dict:
    """Find issues. ``jql`` (raw JQL) OR ``query`` (full-text). ``limit``."""
    jql = (args.get("jql") or "").strip()
    if not jql and args.get("query"):
        q = str(args["query"]).replace('"', '\\"')
        jql = f'text ~ "{q}" ORDER BY updated DESC'
    if not jql:
        return {"ok": False, "error": "missing 'query' or 'jql'"}
    # Scope to the default project when the caller didn't name one — otherwise a
    # bare "text ~ ..." searches every project the token can see (a common cause
    # of a job's filter returning the wrong/empty set). Explicit project=/JQL is
    # left untouched. Honour an explicit args["project"] over the default.
    proj = (args.get("project") or default_project() or "").strip()
    if proj and "project" not in jql.lower():
        low = jql.lower()
        if " order by" in low:
            i = low.index(" order by")
            where, order = jql[:i], jql[i:]
            jql = f'project = "{proj}" AND ({where}){order}'
        else:
            jql = f'project = "{proj}" AND ({jql})'
    # Opt-in time tracking on search hits (original/remaining estimate + spent).
    want_time = _truthy(str(args.get("time", args.get("with_time", "false"))))
    flds = "summary,status,issuetype,assignee"
    if want_time:
        flds += "," + _TIME_FIELDS
    r = _request("GET", "/rest/api/2/search",
                 params={"jql": jql, "maxResults": int(args.get("limit", 10)),
                         "fields": flds})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [_issue_summary(x, with_time=want_time)
           for x in (data.get("issues") or [])]
    return {"ok": True, "results": out, "total": data.get("total")}


def jira_read(args: dict, cwd: str | None = None) -> dict:
    """Read an issue by ``key`` (e.g. ENG-123). Returns fields + comments."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                 params={"fields": "summary,description,status,issuetype,"
                                   "assignee,reporter,priority,labels,comment,"
                                   "attachment," + _TIME_FIELDS})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    comments = [{"author": ((c.get("author") or {}) or {}).get("displayName"),
                 "body": (c.get("body") or "")[:4000]}
                for c in (((f.get("comment") or {}) or {}).get("comments") or [])]
    out = {"ok": True, "key": d.get("key"), "summary": f.get("summary"),
           "type": ((f.get("issuetype") or {}) or {}).get("name"),
           "status": ((f.get("status") or {}) or {}).get("name"),
           "assignee": ((f.get("assignee") or {}) or {}).get("displayName"),
           "reporter": ((f.get("reporter") or {}) or {}).get("displayName"),
           "priority": ((f.get("priority") or {}) or {}).get("name"),
           "labels": f.get("labels") or [],
           "time": _time_fields(f),
           "description": (f.get("description") or "")[:_BODY_CAP],
           "comments": comments, "url": _issue_url(d.get("key", ""))}
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


def jira_worklog(args: dict, cwd: str | None = None) -> dict:
    """Read the time LOGGED against an issue by ``key`` — every worklog entry
    (who, how much, when, comment) plus the estimate/spent rollup. Answers
    "how much time has been recorded on ENG-123 and by whom"."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/worklog",
                 params={"maxResults": int(args.get("limit", 50))})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    logs = []
    total_secs = 0
    for w in (data.get("worklogs") or []):
        secs = w.get("timeSpentSeconds") or 0
        try:
            total_secs += int(secs)
        except (TypeError, ValueError):
            pass
        logs.append({
            "author": ((w.get("author") or {}) or {}).get("displayName"),
            "time_spent": w.get("timeSpent") or _fmt_secs(secs),
            "time_spent_seconds": secs,
            "started": w.get("started"),
            "comment": (w.get("comment") or "")[:500],
        })
    # Estimate/spent rollup for context (one extra lightweight call).
    rollup = None
    tr = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                  params={"fields": _TIME_FIELDS})
    if tr["ok"] and isinstance(tr["data"], dict):
        rollup = _time_fields((tr["data"].get("fields") or {}))
    return {"ok": True, "key": key, "worklogs": logs,
            "worklog_count": len(logs),
            "total_logged": _fmt_secs(total_secs),
            "total_logged_seconds": total_secs,
            "tracking": rollup, "url": _issue_url(key)}


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
        body["comment"] = str(args["comment"])
    if args.get("started"):
        body["started"] = str(args["started"])
    r = _request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/worklog",
                 body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "logged": time_spent,
            "url": _issue_url(key)}


# ─────────────── projects · boards · sprints · dashboards · me ───────────────

def jira_remote_links(args: dict, cwd: str | None = None) -> dict:
    """Remote/web links on an issue (Confluence pages, external URLs). Required:
    ``key``. Returns each link's title + url, and any Confluence page id parsed
    from a wiki URL — the cross-reference a dossier follows into Confluence."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    r = _request("GET",
                 f"/rest/api/2/issue/{urllib.parse.quote(key)}/remotelink")
    if not r["ok"]:
        return r
    import re as _re
    rows = r["data"] if isinstance(r["data"], list) else []
    out = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        obj = it.get("object") or {}
        url = obj.get("url") or ""
        title = obj.get("title") or url
        pid = None
        m = _re.search(r"(?:/pages/|pageId=)(\d{4,})", url)
        if m:
            pid = m.group(1)
        out.append({"title": title, "url": url, "confluence_page_id": pid})
    return {"ok": True, "key": key, "links": out, "count": len(out)}


def jira_myself(args: dict, cwd: str | None = None) -> dict:
    """The authenticated user (name, account id, email) — resolve "me"/"my"."""
    r = _request("GET", "/rest/api/2/myself")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True,
            "display_name": d.get("displayName"),
            "account_id": d.get("accountId") or d.get("key") or d.get("name"),
            "email": d.get("emailAddress"),
            "active": d.get("active")}


def jira_projects(args: dict, cwd: str | None = None) -> dict:
    """List the projects the token can see (key, name, id, lead)."""
    r = _request("GET", "/rest/api/2/project",
                 params={"maxResults": int(args.get("limit", 50))})
    if not r["ok"]:
        return r
    data = r["data"]
    rows = data.get("values") if isinstance(data, dict) else data
    out = [{"key": p.get("key"), "name": p.get("name"), "id": p.get("id"),
            "lead": ((p.get("lead") or {}) or {}).get("displayName")}
           for p in (rows or []) if isinstance(p, dict)]
    return {"ok": True, "projects": out, "count": len(out)}


def jira_boards(args: dict, cwd: str | None = None) -> dict:
    """List Agile boards (scrum/kanban). Optional ``project`` filter. Needs the
    Jira Software (Agile) REST API."""
    params = {"maxResults": int(args.get("limit", 50))}
    proj = (args.get("project") or default_project() or "").strip()
    if proj:
        params["projectKeyOrId"] = proj
    r = _request("GET", "/rest/agile/1.0/board", params=params)
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [{"id": b.get("id"), "name": b.get("name"), "type": b.get("type")}
           for b in (data.get("values") or []) if isinstance(b, dict)]
    return {"ok": True, "boards": out, "count": len(out)}


def jira_sprints(args: dict, cwd: str | None = None) -> dict:
    """List sprints on a board. Required: ``board_id``. Optional ``state``
    (active|closed|future)."""
    bid = str(args.get("board_id") or args.get("board") or "").strip()
    if not bid:
        return {"ok": False, "error": "missing 'board_id'"}
    params = {"maxResults": int(args.get("limit", 50))}
    if args.get("state"):
        params["state"] = str(args["state"])
    r = _request("GET", f"/rest/agile/1.0/board/{urllib.parse.quote(bid)}/sprint",
                 params=params)
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [{"id": s.get("id"), "name": s.get("name"), "state": s.get("state"),
            "start": s.get("startDate"), "end": s.get("endDate")}
           for s in (data.get("values") or []) if isinstance(s, dict)]
    return {"ok": True, "sprints": out, "count": len(out)}


def jira_sprint_issues(args: dict, cwd: str | None = None) -> dict:
    """Issues in a sprint. Required: ``sprint_id``. Optional ``time`` to include
    estimate/spent per issue."""
    sid = str(args.get("sprint_id") or args.get("sprint") or "").strip()
    if not sid:
        return {"ok": False, "error": "missing 'sprint_id'"}
    want_time = _truthy(str(args.get("time", "false")))
    flds = "summary,status,issuetype,assignee"
    if want_time:
        flds += "," + _TIME_FIELDS
    r = _request("GET",
                 f"/rest/agile/1.0/sprint/{urllib.parse.quote(sid)}/issue",
                 params={"maxResults": int(args.get("limit", 50)), "fields": flds})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [_issue_summary(x, with_time=want_time)
           for x in (data.get("issues") or [])]
    return {"ok": True, "sprint_id": sid, "results": out,
            "total": data.get("total")}


def jira_dashboards(args: dict, cwd: str | None = None) -> dict:
    """List dashboards visible to the token (id, name, url)."""
    r = _request("GET", "/rest/api/2/dashboard",
                 params={"maxResults": int(args.get("limit", 50))})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [{"id": d.get("id"), "name": d.get("name"),
            "url": d.get("view") or d.get("self")}
           for d in (data.get("dashboards") or data.get("values") or [])
           if isinstance(d, dict)]
    return {"ok": True, "dashboards": out, "count": len(out)}


def jira_dashboard_read(args: dict, cwd: str | None = None) -> dict:
    """Read one dashboard + its gadgets by ``id``."""
    did = str(args.get("id") or args.get("dashboard_id") or "").strip()
    if not did:
        return {"ok": False, "error": "missing 'id'"}
    r = _request("GET", f"/rest/api/2/dashboard/{urllib.parse.quote(did)}")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    g = _request("GET",
                 f"/rest/api/2/dashboard/{urllib.parse.quote(did)}/gadget")
    gadgets = []
    if g["ok"] and isinstance(g["data"], dict):
        gadgets = [{"title": x.get("title"), "id": x.get("id")}
                   for x in (g["data"].get("gadgets") or [])
                   if isinstance(x, dict)]
    return {"ok": True, "id": d.get("id"), "name": d.get("name"),
            "url": d.get("view") or d.get("self"), "gadgets": gadgets}


def jira_dashboard_create(args: dict, cwd: str | None = None) -> dict:
    """Create a dashboard. Required: ``name``. Optional ``description``,
    ``share`` ('private' default | 'authenticated' | 'global'). Uses the Jira
    Cloud dashboard API (v3); on Jira Server/DC, dashboards can't be created via
    REST — the call returns the server's error so the caller can fall back to
    the UI."""
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    share = (args.get("share") or "private").lower()
    perm = {"private": [],
            "authenticated": [{"type": "authenticated"}],
            "global": [{"type": "global"}]}.get(share, [])
    body = {"name": name,
            "description": str(args.get("description") or ""),
            "sharePermissions": perm}
    r = _request("POST", "/rest/api/3/dashboard", body=body)
    if not r["ok"]:
        # Surface a clear hint when the instance is Server/DC (no create REST).
        r.setdefault("hint", "dashboard create needs Jira Cloud; on Server/DC "
                             "create it in the UI (Dashboards → Create)")
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": d.get("id"), "name": d.get("name"),
            "url": d.get("view") or d.get("self")}


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
        fields["description"] = args["description"]
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


def jira_update(args: dict, cwd: str | None = None) -> dict:
    """Update issue fields. Required: ``key``. Provide any of ``summary``,
    ``description``, ``priority`` (name), ``labels`` (list), ``assignee``
    (name), or a raw ``fields`` dict (merged last, wins)."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    fields: dict = {}
    if args.get("summary"):
        fields["summary"] = args["summary"]
    if args.get("description") is not None:
        fields["description"] = args["description"]
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}
    if args.get("assignee"):
        fields["assignee"] = {"name": args["assignee"]}
    if args.get("labels") is not None:
        labels = args["labels"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]
        fields["labels"] = labels
    if isinstance(args.get("fields"), dict):
        fields.update(args["fields"])
    if not fields:
        return {"ok": False, "error": "no fields to update"}
    r = _request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                 body={"fields": fields})
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "url": _issue_url(key),
            "written": {k: args[k] for k in ("summary", "description",
                        "priority", "labels", "assignee") if args.get(k) is not None}}


def jira_comment(args: dict, cwd: str | None = None) -> dict:
    """Add a comment to an issue. Required: ``key``, ``body``."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    r = _request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                 body={"body": args["body"]})
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
        return {"ok": False, "error": "missing 'key'"}
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
        return {"ok": False, "error": "missing 'key'"}
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
        return {"ok": False, "error": "missing 'key'"}
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
           "jira_comment", "jira_link_issues", "jira_transitions", "jira_transition",
           "jira_assign", "jira_test"]
