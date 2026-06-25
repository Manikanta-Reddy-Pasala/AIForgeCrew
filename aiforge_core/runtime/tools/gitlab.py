"""GitLab (self-managed / SaaS) tool — search / read / create / update issues.

Lets the chat agent pull a GitLab issue in, analyse it, file a new one, edit
fields, or drop a comment (note). REST API v4 (``/api/v4``).

Config (env):
  GITLAB_BASE_URL   e.g. https://gitlab.internal  (no trailing /, no /api/v4)
  GITLAB_TOKEN      Personal / Project / Group Access Token (sent as
                    ``PRIVATE-TOKEN``). An OAuth token works too (set
                    GITLAB_OAUTH=1 → sent as ``Authorization: Bearer``).
  GITLAB_PROJECT    (optional) default project — numeric id or URL path
                    ("group/sub/project"). Used when a call omits ``project``.
  GITLAB_INSECURE_TLS=1   skip TLS verify for a self-signed internal cert

GitLab issues are addressed per-project by their ``iid`` (the number you see
in the UI, e.g. #42), NOT the global id — every read/update/comment needs a
project + iid.

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop.
"""
from __future__ import annotations

import os
import urllib.parse

from . import _http_integration as _http

_TIMEOUT_S = 20
_BODY_CAP = 200_000

_truthy = _http.truthy


def _conf() -> dict:
    """Resolve config: env var WINS, else the UI-persisted store."""
    try:
        from aiforge_core.config import integrations
        stored = integrations.get("gitlab")
    except Exception:  # noqa: BLE001
        stored = {}
    return {
        "base_url": (os.environ.get("GITLAB_BASE_URL")
                     or stored.get("base_url") or "").strip().rstrip("/"),
        # strip whitespace/newlines a pasted token often carries — a stray
        # "\n" in the PRIVATE-TOKEN header value yields a 401.
        "token": (os.environ.get("GITLAB_TOKEN")
                  or stored.get("token") or "").strip(),
        "project": (os.environ.get("GITLAB_PROJECT")
                    or stored.get("project") or "").strip(),
        "oauth": (_truthy(os.environ.get("GITLAB_OAUTH", ""))
                  or bool(stored.get("oauth"))),
        "insecure_tls": (_truthy(os.environ.get("GITLAB_INSECURE_TLS", ""))
                         or bool(stored.get("insecure_tls"))),
    }


def _auth_scheme() -> str:
    return "bearer" if _conf()["oauth"] else "private-token"


def _base() -> str:
    return _conf()["base_url"]


def _api() -> str:
    return _base() + "/api/v4"


def _configured() -> bool:
    c = _conf()
    return bool(c["base_url"] and c["token"])


def _headers() -> dict[str, str]:
    c = _conf()
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "AIForgeCrew-GitLab/1.0"}
    # GitLab accepts a PAT as PRIVATE-TOKEN; an OAuth token as Bearer.
    if c["oauth"]:
        h["Authorization"] = "Bearer " + c["token"]
    else:
        h["PRIVATE-TOKEN"] = c["token"]
    return h


def _ssl_ctx():
    return _http.ssl_context(_conf()["insecure_tls"])


def _proj_id(args: dict | None = None) -> str:
    """Project from the call args, else the configured default."""
    p = ((args or {}).get("project") or (args or {}).get("repo")
         or _conf()["project"] or "")
    return str(p).strip()


def _enc_proj(project: str) -> str:
    """URL-encode a project path ("group/project" → "group%2Fproject").
    A numeric id passes through unchanged."""
    return urllib.parse.quote(str(project), safe="")


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
    if not _configured():
        return {"ok": False, "error": "gitlab_not_configured",
                "hint": "set GITLAB_BASE_URL + GITLAB_TOKEN"}
    url = _api() + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return _http.http_request(method, url, headers=_headers(), body=body,
                              timeout=_TIMEOUT_S, body_cap=_BODY_CAP,
                              context=_ssl_ctx())


def _issue_summary(d: dict) -> dict:
    """Compact one-line view of an issue search hit."""
    if not isinstance(d, dict):
        return {}
    return {
        "iid": d.get("iid"),
        "project_id": d.get("project_id"),
        "title": d.get("title"),
        "state": d.get("state"),
        "labels": d.get("labels") or [],
        "assignee": ((d.get("assignee") or {}) or {}).get("name"),
        "url": d.get("web_url"),
    }


# ─────────────────────────── tools ──────────────────────────────────

def gitlab_search(args: dict, cwd: str | None = None) -> dict:
    """Find issues. ``query`` (or ``search``) full-text. Optional ``project``
    (defaults to GITLAB_PROJECT — omit to search ALL accessible issues),
    ``state`` (opened|closed|all, default all), ``labels`` (comma/list),
    ``limit``."""
    q = (args.get("query") or args.get("search") or "").strip()
    params: dict = {"per_page": int(args.get("limit", 20))}
    if q:
        params["search"] = q
    state = (args.get("state") or "all").strip().lower()
    if state in ("opened", "closed"):
        params["state"] = state
    labels = args.get("labels")
    if labels:
        if isinstance(labels, (list, tuple)):
            labels = ",".join(str(x) for x in labels)
        params["labels"] = labels
    proj = _proj_id(args)
    path = f"/projects/{_enc_proj(proj)}/issues" if proj else "/issues"
    if not proj:
        params["scope"] = "all"
    r = _request("GET", path, params=params)
    if not r["ok"]:
        return r
    rows = r["data"] if isinstance(r["data"], list) else []
    return {"ok": True, "results": [_issue_summary(x) for x in rows]}


def gitlab_read(args: dict, cwd: str | None = None) -> dict:
    """Read an issue by ``project`` + ``iid`` (the #number). Returns fields +
    comments. ``project`` defaults to GITLAB_PROJECT."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or args.get("key") or "").strip()
    if not proj:
        return {"ok": False, "error": "missing 'project'"}
    if not iid:
        return {"ok": False, "error": "missing 'iid'"}
    r = _request("GET", f"/projects/{_enc_proj(proj)}/issues/{urllib.parse.quote(iid)}")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    comments: list = []
    n = _request("GET", f"/projects/{_enc_proj(proj)}/issues/"
                 f"{urllib.parse.quote(iid)}/notes",
                 params={"per_page": 50, "sort": "asc"})
    if n["ok"] and isinstance(n["data"], list):
        comments = [{"author": ((c.get("author") or {}) or {}).get("name"),
                     "body": (c.get("body") or "")[:4000]}
                    for c in n["data"] if not c.get("system")]
    return {"ok": True, "iid": d.get("iid"), "title": d.get("title"),
            "state": d.get("state"),
            "author": ((d.get("author") or {}) or {}).get("name"),
            "assignee": ((d.get("assignee") or {}) or {}).get("name"),
            "labels": d.get("labels") or [],
            "description": (d.get("description") or "")[:_BODY_CAP],
            "comments": comments, "url": d.get("web_url")}


def gitlab_create(args: dict, cwd: str | None = None) -> dict:
    """Create an issue. Required: ``project`` (or GITLAB_PROJECT), ``title``.
    Optional: ``description``, ``labels`` (list/csv), ``assignee_ids`` (list)."""
    proj = _proj_id(args)
    if not proj:
        return {"ok": False, "error": "missing 'project'"}
    if not args.get("title"):
        return {"ok": False, "error": "missing 'title'"}
    body: dict = {"title": args["title"]}
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("labels"):
        labels = args["labels"]
        if isinstance(labels, (list, tuple)):
            labels = ",".join(str(x) for x in labels)
        body["labels"] = labels
    if args.get("assignee_ids"):
        ids = args["assignee_ids"]
        if isinstance(ids, str):
            ids = [int(s) for s in ids.replace(",", " ").split() if s.strip().isdigit()]
        body["assignee_ids"] = list(ids)
    r = _request("POST", f"/projects/{_enc_proj(proj)}/issues", body=body)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": d.get("iid"), "url": d.get("web_url"),
            "written": {"title": args.get("title"), "description": args.get("description")}}


def gitlab_update(args: dict, cwd: str | None = None) -> dict:
    """Update an issue. Required: ``project`` + ``iid``. Provide any of
    ``title``, ``description``, ``labels`` (list/csv), ``state_event``
    (close|reopen), or a raw ``fields`` dict (merged last, wins)."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or args.get("key") or "").strip()
    if not proj:
        return {"ok": False, "error": "missing 'project'"}
    if not iid:
        return {"ok": False, "error": "missing 'iid'"}
    body: dict = {}
    if args.get("title"):
        body["title"] = args["title"]
    if args.get("description") is not None:
        body["description"] = args["description"]
    if args.get("labels") is not None:
        labels = args["labels"]
        if isinstance(labels, (list, tuple)):
            labels = ",".join(str(x) for x in labels)
        body["labels"] = labels
    if args.get("state_event"):
        body["state_event"] = args["state_event"]
    if isinstance(args.get("fields"), dict):
        body.update(args["fields"])
    if not body:
        return {"ok": False, "error": "no fields to update"}
    r = _request("PUT", f"/projects/{_enc_proj(proj)}/issues/"
                 f"{urllib.parse.quote(iid)}", body=body)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": d.get("iid") or iid, "url": d.get("web_url"),
            "written": {k: args[k] for k in ("title", "description", "labels",
                        "state_event") if args.get(k) is not None}}


def gitlab_comment(args: dict, cwd: str | None = None) -> dict:
    """Add a comment (note) to an issue. Required: ``project`` + ``iid``,
    ``body``."""
    proj = _proj_id(args)
    iid = str(args.get("iid") or args.get("id") or args.get("key") or "").strip()
    if not proj:
        return {"ok": False, "error": "missing 'project'"}
    if not iid:
        return {"ok": False, "error": "missing 'iid'"}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    r = _request("POST", f"/projects/{_enc_proj(proj)}/issues/"
                 f"{urllib.parse.quote(iid)}/notes", body={"body": args["body"]})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "iid": iid, "id": d.get("id"),
            "written": {"comment": str(args["body"])[:2000]}}


def gitlab_test() -> dict:
    """Connectivity + auth check for the Settings UI. Hits ``/user`` and, on
    auth failure, explains the most likely cause."""
    if not _configured():
        return {"ok": False, "error": "gitlab_not_configured"}
    scheme = _auth_scheme()
    r = _request("GET", "/user")
    if r.get("ok"):
        d = r["data"] if isinstance(r["data"], dict) else {}
        return {"ok": True, "base_url": _base(), "auth": scheme,
                "user": d.get("username") or d.get("name")}
    err = str(r.get("error", ""))
    out = {**r, "auth": scheme, "base_url": _base()}
    if err.startswith("http 401") or err.startswith("http 403"):
        out["hint"] = ("Token rejected. Use a GitLab Personal/Project/Group "
                       "Access Token with at least 'read_api' (and 'api' for "
                       "writes) scope, not expired; Base URL must be the host "
                       "root (no /api/v4). For an OAuth token, enable the OAuth "
                       "(Bearer) option.")
    return out


__all__ = ["gitlab_search", "gitlab_read", "gitlab_create", "gitlab_update",
           "gitlab_comment", "gitlab_test"]
