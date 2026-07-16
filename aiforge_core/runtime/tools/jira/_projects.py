"""Agile / discovery tools — projects, boards, sprints, dashboards, remote links,
project-name resolution and the authenticated-user lookup.

Split out of the former ``jira.py`` module; behaviour is unchanged.
"""
from __future__ import annotations

import urllib.parse

from ._core import _request, _truthy, default_project
from ._format import _TIME_FIELDS, _issue_summary


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


def jira_resolve_project(args: dict, cwd: str | None = None) -> dict:
    """Resolve a LOOSELY-typed project name/key to the real Jira project key —
    case, spaces, missing hyphens, small typos tolerated. Returns
    ``{ok, key, name, match}`` or candidates when ambiguous/none."""
    name = (args.get("name") or args.get("project") or args.get("query")
            or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    r = jira_projects({"limit": 500}, cwd)
    if not r.get("ok"):
        return r
    cands: dict = {}
    for p in r.get("projects") or []:
        k = p.get("key")
        if not k:
            continue
        cands[k] = k
        if p.get("name"):
            cands[p["name"]] = k
    from aiforge_core.config.repo_map import fuzzy_pick
    return fuzzy_pick(name, cands, value_key="key")


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
