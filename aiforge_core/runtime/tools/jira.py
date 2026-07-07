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
    """Resolve config: env var WINS, else the UI-persisted store."""
    try:
        from aiforge_core.config import integrations
        stored = integrations.get("jira")
    except Exception:  # noqa: BLE001
        stored = {}
    return {
        "base_url": (os.environ.get("JIRA_BASE_URL")
                     or stored.get("base_url") or "").strip().rstrip("/"),
        # strip whitespace/newlines a pasted token often carries — a stray
        # "\n" in the Authorization header value yields a 401.
        "token": (os.environ.get("JIRA_TOKEN")
                  or stored.get("token") or "").strip(),
        "user": (os.environ.get("JIRA_USER") or stored.get("user") or "").strip(),
        "insecure_tls": (_truthy(os.environ.get("JIRA_INSECURE_TLS", ""))
                         or bool(stored.get("insecure_tls"))),
        # Default project key, applied when a call omits ``project`` (auto-fill on
        # create; scope full-text search). env wins, else the UI/chat-persisted
        # store. Lets the user say "use ENG as the default project" once.
        "default_project": (os.environ.get("JIRA_DEFAULT_PROJECT")
                            or stored.get("default_project") or "").strip(),
    }


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


def _fetch_attachments(attachments: list, role: str = "doer") -> list[dict]:
    """Download issue attachments — images AND documents (pdf/xlsx/docx/text) —
    and analyse them (vision caption for images, extracted text for docs) so the
    agent can use them as part of the task. Best-effort, capped, never raises."""
    cap = _max_images()
    if cap <= 0 or not attachments:
        return []
    from aiforge_core.runtime import chat_media
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
            out.append(chat_media.analyze_attachment(name, got["bytes"],
                                                     role, mime=mime))
        except Exception as exc:  # noqa: BLE001
            out.append({"filename": name, "description": "", "error": str(exc)})
    return out


def _issue_summary(d: dict) -> dict:
    """Compact one-line view of an issue search hit."""
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    return {
        "key": d.get("key"),
        "summary": f.get("summary"),
        "type": ((f.get("issuetype") or {}) or {}).get("name"),
        "status": ((f.get("status") or {}) or {}).get("name"),
        "assignee": ((f.get("assignee") or {}) or {}).get("displayName"),
    }


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
    r = _request("GET", "/rest/api/2/search",
                 params={"jql": jql, "maxResults": int(args.get("limit", 10)),
                         "fields": "summary,status,issuetype,assignee"})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [_issue_summary(x) for x in (data.get("issues") or [])]
    return {"ok": True, "results": out, "total": data.get("total")}


def jira_read(args: dict, cwd: str | None = None) -> dict:
    """Read an issue by ``key`` (e.g. ENG-123). Returns fields + comments."""
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    r = _request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                 params={"fields": "summary,description,status,issuetype,"
                                   "assignee,reporter,priority,labels,comment,"
                                   "attachment"})
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
           "description": (f.get("description") or "")[:_BODY_CAP],
           "comments": comments, "url": _issue_url(d.get("key", ""))}
    # Pull attachments (images + documents) + analyse them so the agent uses
    # them as part of the task (opt out with attachments=false). Best-effort.
    if _truthy(str(args.get("attachments", args.get("images", "true")))):
        atts = _fetch_attachments(f.get("attachment") or [])
        if atts:
            out["attachments"] = atts
    return out


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
