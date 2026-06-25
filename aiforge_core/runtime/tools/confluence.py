"""Confluence (Server / Data Center) tool — search / read / create / update pages.

Lets the chat agent pull a page in, analyse it, draft a new page, or edit an
existing one. Server/DC REST API v1 (``/rest/api/content``).

Config (env):
  CONFLUENCE_BASE_URL   e.g. https://confluence.internal  (no trailing /wiki)
  CONFLUENCE_TOKEN      Personal Access Token (Bearer) — or the password/token
                        for basic auth when CONFLUENCE_USER is also set
  CONFLUENCE_USER       (optional) username/email → switches to Basic auth
  CONFLUENCE_INSECURE_TLS=1   skip TLS verify for a self-signed internal cert

Soft-error contract: every function returns ``{"ok": bool, ...}`` and never
raises into the agent loop. Page bodies are Confluence "storage" XHTML.
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
        stored = integrations.get("confluence")
    except Exception:  # noqa: BLE001
        stored = {}
    return {
        "base_url": (os.environ.get("CONFLUENCE_BASE_URL")
                     or stored.get("base_url") or "").strip().rstrip("/"),
        # strip whitespace/newlines a pasted token often carries — a stray
        # "\n" in the Authorization header value yields a 401.
        "token": (os.environ.get("CONFLUENCE_TOKEN")
                  or stored.get("token") or "").strip(),
        "user": (os.environ.get("CONFLUENCE_USER") or stored.get("user") or "").strip(),
        "insecure_tls": (_truthy(os.environ.get("CONFLUENCE_INSECURE_TLS", ""))
                         or bool(stored.get("insecure_tls"))),
    }


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
         "User-Agent": "AIForgeCrew-Confluence/1.0"}
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
        return {"ok": False, "error": "confluence_not_configured",
                "hint": "set CONFLUENCE_BASE_URL + CONFLUENCE_TOKEN"}
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http.http_request(method, url, headers=_headers(), body=body,
                              timeout=_TIMEOUT_S, body_cap=_BODY_CAP,
                              context=_ssl_ctx(),
                              capture_headers=_http.ATLASSIAN_DENIED_HEADERS)


def _page_url(d: dict) -> str:
    links = d.get("_links") if isinstance(d.get("_links"), dict) else {}
    webui = links.get("webui") or ""
    return (_base() + webui) if webui else ""


# ─────────────────────────── tools ──────────────────────────────────

def confluence_search(args: dict, cwd: str | None = None) -> dict:
    """Find pages. ``cql`` (raw CQL) OR ``query`` (full-text). ``limit``."""
    cql = (args.get("cql") or "").strip()
    if not cql and args.get("query"):
        q = str(args["query"]).replace('"', '\\"')
        cql = f'text ~ "{q}"'
    if not cql:
        return {"ok": False, "error": "missing 'query' or 'cql'"}
    r = _request("GET", "/rest/api/content/search",
                 params={"cql": cql, "limit": int(args.get("limit", 10)),
                         "expand": "space,version"})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    out = [{"id": x.get("id"), "title": x.get("title"), "type": x.get("type"),
            "space": (x.get("space") or {}).get("key")}
           for x in (data.get("results") or [])]
    return {"ok": True, "results": out}


def confluence_read(args: dict, cwd: str | None = None) -> dict:
    """Read a page (storage XHTML body). By ``id``, or ``title`` (+ optional
    ``space`` key)."""
    pid = args.get("id")
    if not pid and args.get("title"):
        params = {"title": args["title"], "expand": "version", "limit": 1}
        if args.get("space"):
            params["spaceKey"] = args["space"]
        rr = _request("GET", "/rest/api/content", params=params)
        if not rr["ok"]:
            return rr
        res = (rr["data"].get("results") if isinstance(rr["data"], dict) else None) or []
        if not res:
            return {"ok": False, "error": "page_not_found"}
        pid = res[0].get("id")
    if not pid:
        return {"ok": False, "error": "missing 'id' or 'title'"}
    r = _request("GET", f"/rest/api/content/{pid}",
                 params={"expand": "body.storage,version,space"})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    body = (((d.get("body") or {}).get("storage") or {}).get("value") or "")
    return {"ok": True, "id": d.get("id"), "title": d.get("title"),
            "space": (d.get("space") or {}).get("key"),
            "version": (d.get("version") or {}).get("number"),
            "body": body[:_BODY_CAP], "url": _page_url(d)}


def confluence_create(args: dict, cwd: str | None = None) -> dict:
    """Create a page. Required: ``title``, ``space`` (key), ``body`` (storage
    XHTML). Optional: ``parent_id``, ``representation`` (storage|wiki)."""
    for k in ("title", "space", "body"):
        if not args.get(k):
            return {"ok": False, "error": f"missing '{k}'"}
    payload: dict = {
        "type": "page", "title": args["title"],
        "space": {"key": args["space"]},
        "body": {"storage": {"value": args["body"],
                             "representation": args.get("representation", "storage")}},
    }
    if args.get("parent_id"):
        payload["ancestors"] = [{"id": str(args["parent_id"])}]
    r = _request("POST", "/rest/api/content", body=payload)
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": d.get("id"), "title": d.get("title"),
            "url": _page_url(d),
            "written": {"title": d.get("title") or args["title"],
                        "body": str(args["body"])[:2000]}}


def confluence_update(args: dict, cwd: str | None = None) -> dict:
    """Update a page body. Required: ``id``, ``body``. Optional: ``title``,
    ``representation``. Version is auto-incremented (reads current first)."""
    pid = args.get("id")
    if not pid:
        return {"ok": False, "error": "missing 'id'"}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    cur = _request("GET", f"/rest/api/content/{pid}", params={"expand": "version"})
    if not cur["ok"]:
        return cur
    d = cur["data"] if isinstance(cur["data"], dict) else {}
    next_ver = ((d.get("version") or {}).get("number") or 0) + 1
    title = args.get("title") or d.get("title")
    payload = {
        "type": "page", "title": title,
        "version": {"number": next_ver},
        "body": {"storage": {"value": args["body"],
                             "representation": args.get("representation", "storage")}},
    }
    r = _request("PUT", f"/rest/api/content/{pid}", body=payload)
    if not r["ok"]:
        return r
    rd = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "id": pid, "version": next_ver, "title": title,
            "url": _page_url(rd),
            "written": {"title": title, "body": str(args["body"])[:2000]}}


def confluence_test() -> dict:
    """Connectivity + auth check for the Settings UI. Hits a cheap endpoint
    and, on auth failure, explains the most likely cause."""
    if not _configured():
        return {"ok": False, "error": "confluence_not_configured"}
    scheme = _auth_scheme()
    r = _request("GET", "/rest/api/space", params={"limit": 1})
    if r.get("ok"):
        return {"ok": True, "base_url": _base(), "auth": scheme}
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
            out["hint"] = ("Bearer/PAT rejected. Check the token is a Confluence "
                           "Personal Access Token (not an API key/password), not "
                           "expired, and has read scope; and that Base URL has no "
                           "extra context path (e.g. trailing /wiki is Cloud only).")
    return out


__all__ = ["confluence_search", "confluence_read", "confluence_create",
           "confluence_update", "confluence_test"]
